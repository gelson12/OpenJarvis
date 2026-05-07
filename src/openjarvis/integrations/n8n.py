"""n8n REST API client (workflow CRUD + execution).

Talks to a self-hosted n8n instance over its REST API. Auth uses the
``X-N8N-API-KEY`` header. URL/key come from ``N8N_BASE_URL`` /
``N8N_API_KEY`` (canonical names; the env-alias pass at app startup
populates them from any aliases).

Why a thin wrapper instead of using a generated SDK
---------------------------------------------------
The n8n REST surface is small and stable, and we only need a curated
subset (list/get/create/update workflows, activate, execute, list
executions). Carrying a code-generated SDK would add weight without
proportional benefit.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class N8NUnavailableError(RuntimeError):
    """Raised when the n8n base URL or API key are missing or the call fails."""


class N8NClient:
    """Synchronous httpx-based client for the n8n REST API."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self._base = (
            (base_url or os.environ.get("N8N_BASE_URL", "")).rstrip("/")
        )
        self._key = api_key or os.environ.get("N8N_API_KEY", "")
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._base and self._key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-N8N-API-KEY": self._key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        if not self.configured:
            raise N8NUnavailableError(
                "n8n not configured — set N8N_BASE_URL and N8N_API_KEY"
            )
        url = f"{self._base}/api/v1{path}"
        try:
            with httpx.Client(timeout=self._timeout) as c:
                resp = c.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                )
                resp.raise_for_status()
                return resp.json() if resp.content else None
        except httpx.HTTPError as exc:
            raise N8NUnavailableError(f"n8n {method} {path} failed: {exc}") from exc

    # -- Workflows ------------------------------------------------------

    def list_workflows(self, *, active: Optional[bool] = None, limit: int = 50) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if active is not None:
            params["active"] = "true" if active else "false"
        return self._request("GET", "/workflows", params=params)

    def get_workflow(self, workflow_id: str) -> Any:
        return self._request("GET", f"/workflows/{workflow_id}")

    def create_workflow(self, definition: dict[str, Any]) -> Any:
        return self._request("POST", "/workflows", json_body=definition)

    def update_workflow(self, workflow_id: str, definition: dict[str, Any]) -> Any:
        return self._request("PUT", f"/workflows/{workflow_id}", json_body=definition)

    def activate_workflow(self, workflow_id: str) -> Any:
        return self._request("POST", f"/workflows/{workflow_id}/activate")

    def deactivate_workflow(self, workflow_id: str) -> Any:
        return self._request("POST", f"/workflows/{workflow_id}/deactivate")

    def execute_workflow(
        self,
        workflow_id: str,
        *,
        input_data: Optional[dict[str, Any]] = None,
    ) -> Any:
        return self._request(
            "POST",
            f"/workflows/{workflow_id}/execute",
            json_body=input_data or {},
        )

    def list_executions(
        self,
        *,
        workflow_id: Optional[str] = None,
        limit: int = 20,
    ) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if workflow_id:
            params["workflowId"] = workflow_id
        return self._request("GET", "/executions", params=params)

    # -- Credentials ----------------------------------------------------
    #
    # n8n stores SaaS credentials (Slack, Gmail, Notion, Google Sheets,
    # Stripe, etc.) that workflows reference by id. The REST API only
    # exposes metadata — id, name, type, createdAt, updatedAt — never the
    # encrypted secret values. That's still enough for the OpenJarvis
    # agent to KNOW which SaaS services have a credential available so
    # it can construct or execute a workflow that uses them, instead of
    # asking the user to re-enter the same key in OpenJarvis env.

    def list_credentials(self, *, limit: int = 100) -> Any:
        """List credentials stored in n8n (metadata only — no secret values).

        Returns an array of ``{id, name, type, createdAt, updatedAt}``.
        Use this so the agent knows which SaaS integrations are
        credentialed inside n8n already (Slack, Gmail OAuth, Stripe,
        Notion, etc.) — pair with execute_workflow to actually USE them.
        """
        return self._request(
            "GET", "/credentials", params={"limit": limit},
        )

    def get_credential(self, credential_id: str) -> Any:
        """Fetch a single credential's metadata by id."""
        return self._request("GET", f"/credentials/{credential_id}")

    def list_credential_types(self) -> Any:
        """List the credential schema types n8n knows about (slackApi,
        gmailOAuth2, stripeApi, ...). Useful to discover what kind of
        credential to create for a given SaaS service."""
        return self._request("GET", "/credentials/schema")


_default: Optional[N8NClient] = None


def get_default_client() -> N8NClient:
    global _default
    if _default is None:
        _default = N8NClient()
    return _default


__all__ = ["N8NClient", "N8NUnavailableError", "get_default_client"]
