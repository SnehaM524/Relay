"""Generate the React variant component and the feature-flag config.

Output is a small bundle the engineer drops into the app:
  src/experiments/<Key>/Variant.tsx   the treatment component
  src/experiments/<Key>/index.tsx     flag-gated switch between control and variant
  flags/<key>.json                    provider-specific flag definition (LaunchDarkly / Statsig / GrowthBook / PostHog)

With Claude available the Variant.tsx is written for the specific change; in
mock mode it's a clean, typed scaffold with the change described in-code.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .llm import LLM
from .models import Experiment, Funnel, VariantArtifact

log = logging.getLogger(__name__)

SYSTEM = """You are a senior frontend engineer. Write a production-quality React + TypeScript component
implementing an A/B test treatment. Use functional components, hooks, Tailwind classes, and no external
UI libraries. Fire an analytics event on mount (`track('experiment_exposure', {...})`) and on the primary
action. Keep it self-contained and under 120 lines. Export default the component."""


def pascal(key: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[-_\s]+", key) if w)


def flag_config(provider: str, key: str, exp: Experiment) -> dict[str, Any]:
    p = exp.proposal
    desc = f"{p.name}: {p.hypothesis[:140]}"
    if provider == "launchdarkly":
        return {
            "key": key, "name": p.name, "description": desc, "kind": "boolean", "temporary": True,
            "tags": ["autopilot", p.category, exp.funnel_id],
            "variations": [{"value": False, "name": "control"}, {"value": True, "name": "treatment"}],
            "defaults": {"onVariation": 1, "offVariation": 0},
            "environments": {"production": {"on": True, "fallthrough": {"rollout": {"variations": [
                {"variation": 0, "weight": 50000}, {"variation": 1, "weight": 50000}]}}}},
        }
    if provider == "statsig":
        return {"name": key, "description": desc, "idType": "userID", "groups": [
            {"name": "Control", "size": 50, "parameterValues": {"enabled": False}},
            {"name": "Treatment", "size": 50, "parameterValues": {"enabled": True}}],
            "primaryMetrics": [{"name": f"{p.target_step}_completed", "type": "user"}], "tags": ["autopilot", p.category]}
    if provider == "growthbook":
        return {"key": key, "description": desc, "valueType": "boolean", "defaultValue": "false",
                "environments": {"production": {"enabled": True, "rules": [{"type": "experiment", "trackingKey": key,
                "hashAttribute": "id", "coverage": 1, "values": [{"value": "false", "weight": 0.5}, {"value": "true", "weight": 0.5}]}]}}}
    if provider == "posthog":
        return {"key": key, "name": p.name, "active": True, "filters": {"multivariate": {"variants": [
            {"key": "control", "rollout_percentage": 50}, {"key": "test", "rollout_percentage": 50}]}},
            "experiment": {"name": p.name, "description": desc, "parameters": {"feature_flag_variants": ["control", "test"]}}}
    raise ValueError(f"unknown flag provider {provider}")


def _mock_variant(component: str, exp: Experiment) -> str:
    p = exp.proposal
    return f'''import {{ useEffect }} from "react";
import {{ track }} from "@/lib/analytics";

/**
 * {p.name}
 * Experiment: {exp.id}
 * Hypothesis: {p.hypothesis}
 * Change: {p.change}
 * Target step: {p.target_step}
 */
export interface {component}Props {{
  onPrimaryAction: () => void;
}}

export default function {component}({{ onPrimaryAction }}: {component}Props) {{
  useEffect(() => {{
    track("experiment_exposure", {{ experiment: "{exp.id}", variant: "treatment", step: "{p.target_step}" }});
  }}, []);

  const handleClick = () => {{
    track("experiment_primary_action", {{ experiment: "{exp.id}", variant: "treatment" }});
    onPrimaryAction();
  }};

  return (
    <section data-experiment="{exp.id}" className="mx-auto max-w-xl space-y-6 p-8">
      {{/* TODO (autopilot): implement: {p.change} */}}
      <h1 className="text-3xl font-semibold tracking-tight">{p.name}</h1>
      <p className="text-base text-slate-600">{p.hypothesis}</p>
      <button
        onClick={{handleClick}}
        className="rounded-lg bg-indigo-600 px-5 py-3 text-white hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-500"
      >
        Continue
      </button>
    </section>
  );
}}
'''


def _switch(component: str, key: str, exp: Experiment, provider: str) -> str:
    imp, hook = {
        "launchdarkly": ('import { useFlags } from "launchdarkly-react-client-sdk";', f'const useTreatment = () => Boolean(useFlags()["{key}"]);'),
        "statsig": ('import { useExperiment } from "statsig-react";', f'const useTreatment = () => Boolean(useExperiment("{key}").config.get("enabled", false));'),
        "growthbook": ('import { useFeatureIsOn } from "@growthbook/growthbook-react";', f'const useTreatment = () => useFeatureIsOn("{key}");'),
        "posthog": ('import { useFeatureFlagVariantKey } from "posthog-js/react";', f'const useTreatment = () => useFeatureFlagVariantKey("{key}") === "test";'),
    }[provider]
    return f'''{imp}
import Control from "./Control";
import Variant from "./Variant";

{hook}

/** Flag-gated switch for experiment {exp.id} ({exp.proposal.name}). Delete after readout. */
export default function {component}Experiment(props: React.ComponentProps<typeof Control>) {{
  const treatment = useTreatment();
  return treatment ? <Variant {{...props}} /> : <Control {{...props}} />;
}}
'''


class Generator:
    def __init__(self, llm: LLM, provider: str = "launchdarkly"):
        self.llm = llm
        self.provider = provider

    def generate(self, exp: Experiment, funnel: Funnel) -> VariantArtifact:
        p = exp.proposal
        component = pascal(p.key)
        key = f"exp-{p.key}"
        code = self._llm_variant(component, exp, funnel) or _mock_variant(component, exp)
        cfg = flag_config(self.provider, key, exp)
        files = {
            f"src/experiments/{component}/Variant.tsx": code,
            f"src/experiments/{component}/index.tsx": _switch(component, key, exp, self.provider),
            f"flags/{key}.json": __import__("json").dumps(cfg, indent=2),
        }
        return VariantArtifact(component_name=component, react_code=code, flag_key=key, flag_config=cfg, files=files,
                               notes="Control.tsx = your existing component; wire index.tsx in place of it.")

    def _llm_variant(self, component: str, exp: Experiment, funnel: Funnel) -> str | None:
        if not self.llm.enabled:
            return None
        p = exp.proposal
        user = (f"Product: {funnel.product}\nContext: {funnel.context}\n\nExperiment: {p.name}\nHypothesis: {p.hypothesis}\n"
                f"Change to implement: {p.change}\nFunnel step: {p.target_step}\nComponent name: {component}\nExperiment id: {exp.id}\n\n"
                "Write Variant.tsx. Return only the code.")
        try:
            out = self.llm.text(SYSTEM, user)
            if out:
                out = re.sub(r"^```(?:tsx|typescript|jsx)?\s*|\s*```$", "", out.strip())
            return out or None
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM variant generation failed (%s); using scaffold", exc)
            return None
