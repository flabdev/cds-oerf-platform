const ROLE_LABELS = {
  tenant_admin: "Tenant admin",
  platform_admin: "Platform admin",
  ops_user: "Ops user",
};

export function displayName(principal) {
  if (!principal?.authenticated) {
    return "Guest";
  }
  const name = typeof principal.name === "string" ? principal.name.trim() : "";
  if (name) {
    return name;
  }
  return ROLE_LABELS[principal.role] || "Signed-in user";
}

export function sessionView(principal) {
  if (!principal?.authenticated) {
    return { state: "signed-out", displayName: "Guest" };
  }
  return {
    state: "ready",
    tenantId: principal.tenantId,
    role: principal.role,
    displayName: displayName(principal),
    canAdmin: principal.role === "tenant_admin" || principal.role === "platform_admin",
    canSwitchTenant: principal.role === "platform_admin",
  };
}

export function incidentListView(incidents) {
  if (incidents == null) {
    return { state: "loading" };
  }
  if (incidents.error) {
    return { state: "error", message: incidents.error };
  }
  if (incidents.length === 0) {
    return { state: "empty" };
  }
  return { state: "ready", incidents };
}

export function actionRequest(incidentId, action, optionId, actionId, engagementId) {
  const body = { action, action_id: actionId };
  if (engagementId) {
    body.engagement_id = engagementId;
  }
  if (optionId) {
    body.option_id = optionId;
  }
  return {
    method: "POST",
    path: `/incidents/${incidentId}/action`,
    idempotencyKey: actionId,
    body,
  };
}

export function allowanceDraft(published, allowance) {
  const next = structuredClone(published);
  next.product_profiles[0].excursion_allowance_min = Number(allowance);
  return next;
}

export function rankingNote(allowance) {
  if (Number(allowance) <= 30) {
    return "At 30 minutes, facility B is outside the allowance.";
  }
  return "At 45 minutes, facility B is inside the allowance.";
}
