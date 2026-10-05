"""
FHIR read wire-adapter replacing the legacy fhir-server's Observation search
for uhis_lf_mobile -- auth only; the actual FHIR resource synthesis lives in
spice_next_core.api.fhir (invariant: FHIR is egress-only, nothing is stored
as FHIR).

  shukhee_integration.api.fhir_proxy.observation   GET /fhir-server/fhir/Observation?encounter=Encounter/{id}
"""

import frappe
from frappe import _

from spice_next_core.api.fhir import observations_by_encounter
from spice_next_core.auth.decorators import whitelist


@whitelist(methods=["GET"], remote_auth=True)
def observation(encounter=None):
	if not encounter:
		frappe.throw(_("encounter is required."), frappe.ValidationError)
	# Wire value is "Encounter/{id}" (a FHIR reference literal); we only ever
	# need the bare id to look up the Observation rows.
	encounter_id = encounter.split("/", 1)[-1]
	return observations_by_encounter(encounter_id)
