"""Shared name for the dashboard app client id published to SSM.

The client is created in ``OerfApiStack`` because its callback URL is the
CloudFront domain. ``OerfComputeStack`` already feeds that stack, so the
authorizer cannot take the client id as an environment token. It reads this
parameter at cold start instead.
"""

DASHBOARD_CLIENT_PARAMETER = "/oerf/ws-allowed-clients/dashboard"
