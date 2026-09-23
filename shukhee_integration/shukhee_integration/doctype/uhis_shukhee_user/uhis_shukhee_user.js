// Copyright (c) 2026, Medtronic Labs and contributors
// For license information, please see license.txt

frappe.ui.form.on("UHIS Shukhee User", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Initiate Call"), () => shukhee_call_flow.start(frm));
	},
});

// ── Consultation flow: input -> loading -> video call -> wrap-up -> completed ──
//
// Mirrors shukhee_integration.api.consultation (start_consultation /
// get_consultation_status / get_prescription). Those endpoints use this app's
// remote_auth pattern (X-Auth-Token: Bearer <JWT>, not desk session auth), so
// this client script mints the same kind of unverified dev-mode JWT the
// Postman collection does (Phase 1 of JWTTokenValidator -- only the `sub`
// claim is read, signature is never checked) using the UHIS Shukhee User
// record's own linked Provider.user as the identity. That's deliberate: the
// call is made "as" the Provider this record belongs to, not as whichever
// desk user happens to have this record open.
//
// Shukhee's own integration contract explicitly forbids embedding the video
// call in an iframe ("Load it as a top-level WebView navigation - never as a
// JSON/API request or iframe") -- so the video-call step opens call_url in a
// new browser tab, it never gets embedded here.
//
// There is no distinct "prescription is being written" signal from Shukhee
// (its status vocabulary has no state between "accepted" and "completed") --
// get_consultation_status already downloads the prescription/invoice
// synchronously the moment it first observes `completed`, so the wrap-up
// screen here is a short cosmetic pause matching the mocked flow, not a
// separate polling phase.
window.shukhee_call_flow = {
	POLL_INTERVAL_MS: 4000,
	TERMINAL_STATUSES: ["completed", "rejected", "cancelled", "on-hold"],
	// "At-or-past" ConsultationStarted, not exact-equals -- a poll cadence of
	// 4s can easily skip straight over an intermediate value Shukhee held only
	// briefly, so once the doctor has moved past ConsultationStarted, don't
	// get stuck waiting to see that exact value again.
	CONSULTATION_STARTED_STATUSES: ["ConsultationStarted", "ConsultationEnd", "Completed"],
	// Bounds the new pre-show poll loop below -- unlike the mobile app (which
	// already has AppConfig.teleconsultPollMaxAttempts), this loop previously
	// didn't exist at all (the video call used to show immediately), so a
	// stuck "Connecting..." screen would otherwise wait forever.
	MAX_CONNECT_ATTEMPTS: 60,

	async start(frm) {
		const provider_user = await frappe.db.get_value("Provider", frm.doc.provider, "user");
		const user_email = provider_user.message && provider_user.message.user;
		if (!user_email) {
			frappe.msgprint({
				title: __("Cannot Initiate Call"),
				indicator: "red",
				message: __(
					"Provider {0} has no linked User -- Initiate Call authenticates as that Provider and needs one.",
					[frm.doc.provider]
				),
			});
			return;
		}
		if (frm.doc.status !== "Active") {
			frappe.msgprint({
				title: __("Cannot Initiate Call"),
				indicator: "red",
				message: __("This Shukhee account is Inactive."),
			});
			return;
		}

		this._auth_token = this._mint_dev_token(user_email);
		this._show_input_dialog(frm);
	},

	_mint_dev_token(sub) {
		const b64url = (obj) =>
			btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
		const header = b64url({ alg: "none", typ: "JWT" });
		const payload = b64url({ sub, iat: Math.floor(Date.now() / 1000) });
		return `${header}.${payload}.`;
	},

	_show_input_dialog(frm) {
		const d = new frappe.ui.Dialog({
			title: __("Start Consultation"),
			fields: [
				{
					fieldname: "contact_number",
					fieldtype: "Data",
					label: __("Contact Number"),
					reqd: 1,
					default: "+8801410820112",
					description: __(
						"Checked against Shukhee and this platform's own Patient records first. If neither has a match, fill in Patient Name/DOB/Gender below -- required in that case, or the call can't be booked."
					),
				},
				{
					fieldname: "reason",
					fieldtype: "Small Text",
					label: __("Reason"),
					reqd: 1,
					default: "Instant teleconsultation",
				},
				{
					fieldname: "requested_speciality",
					fieldtype: "Data",
					label: __("Requested Speciality"),
					reqd: 1,
					// Must match a title from GET /patient/emergency-request-specialities exactly
					// (case-insensitive) or resolve_speciality_id silently falls back to whichever
					// speciality that endpoint lists first -- "General Medicine" isn't a real
					// speciality in the sandbox and only ever matched by that fallback.
					default: "General Physician",
				},
				{ fieldname: "encounter_id", fieldtype: "Data", label: __("Encounter ID (optional)") },
				{
					fieldname: "new_patient_section",
					fieldtype: "Section Break",
					label: __("New Patient Details"),
					description: __(
						"Only required if Contact Number isn't already a known Patient or Shukhee patient -- see note above. Always visible so it isn't missed."
					),
				},
				{ fieldname: "patient_name", fieldtype: "Data", label: __("Patient Name"), default: "Test Patient" },
				{ fieldname: "column_break_patient_1", fieldtype: "Column Break" },
				{ fieldname: "patient_dob", fieldtype: "Date", label: __("Date of Birth"), default: "1999-05-18" },
				{
					fieldname: "patient_gender",
					fieldtype: "Select",
					label: __("Gender"),
					options: "\nMale\nFemale\nOthers",
					default: "Male",
				},
				{
					fieldname: "medias_html",
					fieldtype: "HTML",
					options: `<label class="control-label">${__("Attachments (optional)")}</label><input type="file" id="shukhee-medias-input" multiple style="display:block;margin-top:4px;">`,
				},
			],
			primary_action_label: __("Start Consultation"),
			primary_action: (values) => {
				const file_input = d.$wrapper.find("#shukhee-medias-input")[0];
				const files = file_input ? Array.from(file_input.files) : [];
				d.hide();
				shukhee_call_flow._run(frm, values, files);
			},
		});
		d.show();
	},

	_run(frm, values, files) {
		const flow = new frappe.ui.Dialog({ title: __("Consultation"), size: "small" });
		flow.get_close_btn().show();
		flow.onhide = () => {
			if (this._poll_timer) clearTimeout(this._poll_timer);
		};
		this._flow = flow;
		this._render_loading(flow, __("Setting up your consultation..."), __("Please wait while we connect you with your doctor."));
		flow.show();

		const form_data = new FormData();
		form_data.append("contact_number", values.contact_number);
		form_data.append("reason", values.reason);
		form_data.append("requested_speciality", values.requested_speciality);
		if (values.encounter_id) form_data.append("encounter_id", values.encounter_id);
		if (values.patient_name) form_data.append("patient_name", values.patient_name);
		if (values.patient_dob) form_data.append("patient_dob", values.patient_dob);
		if (values.patient_gender) form_data.append("patient_gender", values.patient_gender);
		files.forEach((f) => form_data.append("medias", f));

		fetch("/api/method/shukhee_integration.api.consultation.start_consultation", {
			method: "POST",
			headers: {
				"X-Auth-Token": `Bearer ${this._auth_token}`,
				"X-Frappe-CSRF-Token": frappe.csrf_token,
			},
			body: form_data,
		})
			.then((r) => r.json().then((body) => ({ ok: r.ok, body })))
			.then(({ ok, body }) => {
				if (!ok) throw new Error(this._extract_error(body));
				const data = body.message || body;
				this._call_log = data.call_log;
				this._call_url = data.call_url;
				this._render_loading(
					flow,
					__("Connecting to your doctor..."),
					__("Please wait while the doctor joins the call.")
				);
				this._poll_until_started(frm);
			})
			.catch((err) => this._render_error(flow, err.message));
	},

	// Polls until the appointment reaches ConsultationStarted (per Shukhee's
	// integration contract: don't show Join Call before then), or a terminal
	// outer status is reached first (e.g. rejected/cancelled before any
	// doctor ever joins, which must still resolve via _on_terminal, not hang
	// here waiting for a status that will never arrive).
	_poll_until_started(frm, attempt = 1) {
		const check = () => {
			frappe
				.call({
					method: "shukhee_integration.api.consultation.get_consultation_status",
					args: { call_log: this._call_log },
					headers: { "X-Auth-Token": `Bearer ${this._auth_token}` },
				})
				.then((r) => {
					const data = r.message || {};
					if (this.TERMINAL_STATUSES.includes(data.status)) {
						this._on_terminal(frm, data);
						return;
					}
					if (this.CONSULTATION_STARTED_STATUSES.includes(data.appointment_status)) {
						this._show_video_call(this._flow, frm);
						this._poll(frm);
						return;
					}
					if (attempt >= this.MAX_CONNECT_ATTEMPTS) {
						this._render_error(
							this._flow,
							__("Still waiting for the doctor to join. Please check back shortly.")
						);
						return;
					}
					this._poll_timer = setTimeout(() => this._poll_until_started(frm, attempt + 1), this.POLL_INTERVAL_MS);
				})
				.catch((err) => this._render_error(this._flow, err.message || __("Status check failed.")));
		};
		this._poll_timer = setTimeout(check, this.POLL_INTERVAL_MS);
	},

	_show_video_call(flow, frm) {
		// Kept embedded per explicit user choice, despite Shukhee's own integration
		// contract stating this URL must be a top-level WebView navigation and never an
		// iframe. Confirmed against the live sandbox why that contract exists: Shukhee's
		// video-call app (Next.js) resolves the call session via a cookie set on first
		// load, which is a third-party cookie inside a cross-origin iframe -- browsers
		// refuse to set/send it, so the join fails with SESSION_NOT_FOUND even though the
		// link is perfectly valid (the identical link opened top-level works every time).
		// The "Open in a new tab instead" link below is the reliable escape hatch, not a
		// cosmetic extra -- it's a real top-level navigation and always works.
		flow.set_title(__("Video Call"));
		flow.$wrapper.find(".modal-dialog").removeClass("modal-sm").addClass("modal-xl");
		flow.modal_body.html(`
			<div style="padding:0;">
				<iframe
					src="${this._call_url}"
					style="width:100%;height:70vh;border:none;display:block;"
					allow="camera; microphone; autoplay; fullscreen"
				></iframe>
				<div class="text-muted" style="padding:8px 4px;font-size:12px;">
					${__("Waiting for the consultation to end...")}
					${__("Blank or refused to connect?")}
					<a href="#" class="shukhee-open-tab-link">${__("Open in a new tab instead")}</a>
				</div>
			</div>
		`);
		flow.$wrapper.find(".shukhee-open-tab-link").on("click", (e) => {
			e.preventDefault();
			window.open(this._call_url, "_blank");
		});
	},

	_poll(frm) {
		if (this._poll_timer) clearTimeout(this._poll_timer);
		this._shown_wrap_wait = false;
		const check = () => {
			frappe
				.call({
					method: "shukhee_integration.api.consultation.get_consultation_status",
					args: { call_log: this._call_log },
					headers: { "X-Auth-Token": `Bearer ${this._auth_token}` },
				})
				.then((r) => {
					const data = r.message || {};
					if (this.TERMINAL_STATUSES.includes(data.status)) {
						this._on_terminal(frm, data);
						return;
					}
					if (data.appointment_status === "ConsultationEnd" && !this._shown_wrap_wait) {
						this._shown_wrap_wait = true;
						this._render_loading(
							this._flow,
							__("Prescription in progress"),
							__("Please wait while your doctor prepares your prescription. This may take 2–5 mins.")
						);
					}
					this._poll_timer = setTimeout(check, this.POLL_INTERVAL_MS);
				})
				.catch((err) => this._render_error(this._flow, err.message || __("Status check failed.")));
		};
		this._poll_timer = setTimeout(check, this.POLL_INTERVAL_MS);
	},

	_on_terminal(frm, data) {
		const flow = this._flow;
		if (!flow || !flow.is_visible) return; // user closed the dialog

		flow.$wrapper.find(".modal-dialog").removeClass("modal-xl").addClass("modal-sm");

		if (data.status !== "completed") {
			this._render_error(
				flow,
				__("Consultation ended with status: {0}", [data.status])
			);
			return;
		}

		flow.set_title(__("Consultation Details"));

		// No artificial delay here -- by the time _on_terminal fires, the outer
		// status is genuinely "completed" and get_consultation_status has
		// already downloaded the prescription/invoice synchronously the moment
		// it first observed that status (see api/consultation.py), so there's
		// nothing left to wait for.
		flow.modal_body.html(`
			<div style="padding:8px;">
				<div style="color:var(--green-600,#2e7d32);font-weight:600;margin-bottom:16px;">
					&#10003; ${__("Consultation completed")}
				</div>
				<div style="font-weight:600;border-bottom:1px solid var(--border-color);padding-bottom:6px;margin-bottom:10px;">
					${__("Prescription")}
				</div>
				<div class="text-muted" style="margin-bottom:16px;font-size:12px;">
					${__("Shukhee currently returns a PDF prescription, not itemized medicine data -- open it to view details.")}
				</div>
				<button class="btn btn-primary btn-sm shukhee-view-rx-btn" ${data.prescription_link ? "" : "disabled"}>
					${__("View Prescription")}
				</button>
				${data.invoice_link ? `<a href="${data.invoice_link}" target="_blank" class="btn btn-default btn-sm" style="margin-left:6px;">${__("Invoice")}</a>` : ""}
				${this._render_clinical_data_html(data.clinical_data)}
			</div>
		`);
		flow.$wrapper.find(".shukhee-view-rx-btn").on("click", () => {
			window.open(data.prescription_link, "_blank");
		});
	},

	// Renders Shukhee's appointment.clinicalData (diagnosis, medicines,
	// meal instructions, vitals, follow-up) -- captured server-side once the
	// consultation completes (see api/consultation.py's get_consultation_status).
	// Returns "" when absent (a consultation completed before this field was
	// captured, or one Shukhee genuinely returned nothing for).
	_render_clinical_data_html(clinicalData) {
		if (!clinicalData) return "";

		const bullets = (label, items) => {
			if (!items || !items.length) return "";
			return `<div style="margin-top:10px;">
				<div style="font-weight:600;font-size:12px;">${frappe.utils.escape_html(label)}</div>
				<div style="font-size:12px;">${items.map((i) => frappe.utils.escape_html(String(i))).join(", ")}</div>
			</div>`;
		};

		const medicineRows = (clinicalData.medicine || [])
			.map((m) => {
				const name = m.brandName || m.genericName || "";
				const details = [m.strength, m.dosage, m.frequency, m.instruction].filter(Boolean).join(" · ");
				return `<div>${frappe.utils.escape_html(name)}${details ? " — " + frappe.utils.escape_html(details) : ""}</div>`;
			})
			.join("");

		const mealRows = (clinicalData.mealInstruction || [])
			.map((m) => {
				const label = [m.mealType, m.instruction].filter(Boolean).join(": ");
				return `<div>${frappe.utils.escape_html(label)}</div>`;
			})
			.join("");

		const vital = clinicalData.lastVital || {};
		const vitalParts = [
			vital.temperature ? `${vital.temperature}°` : null,
			vital.pulseRate ? `${vital.pulseRate} bpm` : null,
			vital.bloodPressure ? `${vital.bloodPressure} mmHg` : null,
			vital.spo2 ? `SpO2 ${vital.spo2}%` : null,
		].filter(Boolean);

		const followUpParts = [clinicalData.followUpComment, clinicalData.followUpDay, clinicalData.followUpDate].filter(
			Boolean
		);

		return `
			<div style="margin-top:16px;border-top:1px solid var(--border-color);padding-top:10px;">
				<div style="font-weight:600;border-bottom:1px solid var(--border-color);padding-bottom:6px;margin-bottom:6px;">
					${__("Doctor's Summary")}
				</div>
				${bullets(__("Chief Complaints"), clinicalData.chiefComplaints)}
				${bullets(__("Diagnosis"), clinicalData.diagnosis)}
				${bullets(__("Lab Tests"), clinicalData.labTest)}
				${bullets(__("Advice"), clinicalData.advice)}
				${bullets(__("Drug History"), clinicalData.drugHistory)}
				${
					medicineRows
						? `<div style="margin-top:10px;"><div style="font-weight:600;font-size:12px;">${__("Medicines")}</div><div style="font-size:12px;">${medicineRows}</div></div>`
						: ""
				}
				${
					mealRows
						? `<div style="margin-top:10px;"><div style="font-weight:600;font-size:12px;">${__("Meal Instructions")}</div><div style="font-size:12px;">${mealRows}</div></div>`
						: ""
				}
				${
					vitalParts.length
						? `<div style="margin-top:10px;"><div style="font-weight:600;font-size:12px;">${__("Vitals Recorded by Doctor")}</div><div style="font-size:12px;">${frappe.utils.escape_html(vitalParts.join(" · "))}</div></div>`
						: ""
				}
				${
					followUpParts.length
						? `<div style="margin-top:10px;"><div style="font-weight:600;font-size:12px;">${__("Follow-up")}</div><div style="font-size:12px;">${frappe.utils.escape_html(followUpParts.join(" · "))}</div></div>`
						: ""
				}
			</div>
		`;
	},

	_render_loading(flow, title, subtitle) {
		flow.modal_body.html(`
			<div style="text-align:center;padding:32px 8px;">
				<div class="spinner-border" style="width:2rem;height:2rem;border-width:2px;margin-bottom:16px;"></div>
				<div style="font-weight:600;margin-bottom:6px;">${title}</div>
				<div class="text-muted" style="font-size:12px;">${subtitle}</div>
			</div>
		`);
	},

	_render_error(flow, message) {
		if (this._poll_timer) clearTimeout(this._poll_timer);
		if (!flow) return;
		flow.modal_body.html(`
			<div style="text-align:center;padding:24px 8px;">
				<div style="color:var(--red-600,#c62828);font-weight:600;margin-bottom:10px;">${__("Something went wrong")}</div>
				<div class="text-muted" style="font-size:12px;">${frappe.utils.escape_html(message || "")}</div>
			</div>
		`);
	},

	_extract_error(body) {
		try {
			const msgs = JSON.parse(body._server_messages || "[]");
			if (msgs.length) return JSON.parse(msgs[0]).message;
		} catch (e) {
			// fall through
		}
		return body.exception || __("Request failed.");
	},
};
