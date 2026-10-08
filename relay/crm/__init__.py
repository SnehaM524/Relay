from .hubspot import HubSpotClient, MockHubSpotClient, parse_hubspot_webhook, verify_hubspot_signature
from .salesforce import MockSalesforceClient, SalesforceClient

__all__ = [
    "HubSpotClient",
    "MockHubSpotClient",
    "parse_hubspot_webhook",
    "verify_hubspot_signature",
    "SalesforceClient",
    "MockSalesforceClient",
]
