// Two tabs (see call_logs.json's field_order): "Summary" -- a custom, modern at-a-glance
// dashboard built entirely by this script into the `summary_html` field -- and "Details",
// the full set of raw fields (unchanged), just nested under its own Tab Break.
//
// Within Details, `medias` (the Call Log Media child table) still renders as a grouped
// thumbnail gallery instead of Frappe's default spreadsheet-style grid (into
// `medias_gallery`), and `prescription_link` still gets an inline PDF viewer (into
// `prescription_preview`) -- both untouched from before. Summary reuses the same
// rendering helpers so the two tabs never drift out of sync with each other.
frappe.ui.form.on("Call Logs", {
	refresh(frm) {
		render_summary(frm);
		render_medias_gallery(frm);
		render_prescription_preview(frm);
		bind_media_preview_clicks(frm);
	},
});

// A4 portrait (210x297mm) aspect ratio, sized off width rather than a fixed pixel height --
// so page 1 of the prescription PDF actually shows as a full, correctly-proportioned page
// (not squashed/cropped) at any container width, instead of a fixed height that happened
// to match neither a phone-photographed document nor a real A4 print.
const CALL_LOG_A4_IFRAME_STYLE =
	"width: 100%; max-width: 700px; aspect-ratio: 210 / 297; border: 1px solid var(--border-color); border-radius: 8px; display: block; margin: 0 auto;";

// Delegated (not inline onclick) -- a doctype client script's top-level functions aren't
// guaranteed to land on `window` in every Frappe build, which silently breaks inline
// onclick="..." attributes with no visible error. Delegating on frm.$wrapper instead
// works regardless of that, and `.off` before `.on` keeps this idempotent across the
// repeated refresh() calls that re-render the gallery HTML from scratch each time.
function bind_media_preview_clicks(frm) {
	frm.$wrapper
		.off("click.call-log-media-preview")
		.on("click.call-log-media-preview", ".call-log-media-card", function () {
			const index = parseInt($(this).attr("data-index"), 10);
			if (!isNaN(index)) call_log_open_media_preview(frm.doc.medias || [], index);
		});
}

const CALL_LOG_MEDIA_TYPE_LABELS = {
	prescription: __("Prescription"),
	lab_report: __("Lab Report"),
	other: __("Other"),
};

const CALL_LOG_MEDIA_TYPE_INDICATOR = {
	prescription: "blue",
	lab_report: "orange",
	other: "gray",
};

const CALL_LOG_STATUS_INDICATOR = {
	pending: "gray",
	confirmed: "blue",
	accepted: "orange",
	completed: "green",
	rejected: "red",
	cancelled: "red",
	"on-hold": "orange",
};

function humanize_status(status) {
	return (status || "")
		.split(/[-_]/)
		.filter(Boolean)
		.map((word) => word.charAt(0).toUpperCase() + word.slice(1))
		.join(" ");
}

function render_summary(frm) {
	const wrapper = frm.fields_dict.summary_html && frm.fields_dict.summary_html.$wrapper;
	if (!wrapper) return;

	const doc = frm.doc;
	const status = doc.status || "pending";
	const indicator = CALL_LOG_STATUS_INDICATOR[status] || "gray";
	const doctor_line = [doc.doctor_name, doc.doctor_speciality, doc.doctor_facility]
		.filter(Boolean)
		.join(" · ");

	const info_rows = [
		[__("Contact Number"), doc.contact_number],
		[__("Requested Speciality"), doc.requested_speciality],
		[__("Doctor"), doctor_line || __("Not yet assigned")],
		[__("Call Type"), doc.call_type ? humanize_status(doc.call_type) : null],
		[__("Shukhee User"), doc.shukhee_user],
		[__("UHIS User"), doc.uhis_user],
	].filter(([, value]) => value);

	const info_grid = info_rows
		.map(
			([label, value]) => `
				<div>
					<div class="call-log-summary-label">${frappe.utils.escape_html(label)}</div>
					<div class="call-log-summary-value">${frappe.utils.escape_html(String(value))}</div>
				</div>
			`
		)
		.join("");

	const media_html = render_media_groups(group_medias(doc.medias || []));

	const prescription_html = doc.prescription_link
		? `<iframe
				src="${frappe.utils.escape_html(doc.prescription_link)}"
				style="${CALL_LOG_A4_IFRAME_STYLE}"
			></iframe>`
		: `<div class="text-muted">${__("Not available yet.")}</div>`;

	const invoice_html = doc.invoice_link
		? `<a href="${frappe.utils.escape_html(doc.invoice_link)}" target="_blank" class="btn btn-default btn-sm">
				${__("View Invoice")}
			</a>`
		: "";

	wrapper.html(`
		<style>
			.call-log-summary-card {
				background: var(--card-bg, var(--fg-color));
				border: 1px solid var(--border-color);
				border-radius: 10px;
				padding: 20px;
				margin-bottom: 16px;
			}
			.call-log-summary-header {
				display: flex;
				align-items: center;
				justify-content: space-between;
				flex-wrap: wrap;
				gap: 8px;
				margin-bottom: 16px;
			}
			.call-log-summary-title { font-size: 18px; font-weight: 700; }
			.call-log-summary-grid {
				display: grid;
				grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
				gap: 16px;
			}
			.call-log-summary-label {
				font-size: 11px;
				text-transform: uppercase;
				letter-spacing: 0.03em;
				color: var(--text-muted);
				margin-bottom: 4px;
			}
			.call-log-summary-value { font-size: 14px; font-weight: 600; }
			.call-log-summary-section-title {
				font-size: 13px;
				font-weight: 700;
				display: flex;
				align-items: center;
				justify-content: space-between;
				margin-bottom: 12px;
			}
		</style>
		<div class="call-log-summary-card">
			<div class="call-log-summary-header">
				<div class="call-log-summary-title">
					${frappe.utils.escape_html(doc.contact_number || __("Call Log"))}
				</div>
				<span class="indicator-pill ${indicator}">${frappe.utils.escape_html(humanize_status(status))}</span>
			</div>
			<div class="call-log-summary-grid">${info_grid}</div>
		</div>
		<div class="call-log-summary-card">
			<div class="call-log-summary-section-title">${__("Medical Documents")}</div>
			${media_html}
		</div>
		<div class="call-log-summary-card">
			<div class="call-log-summary-section-title">
				<span>${__("Prescription")}</span>
				${invoice_html}
			</div>
			${prescription_html}
		</div>
	`);
}

function render_prescription_preview(frm) {
	const wrapper = frm.fields_dict.prescription_preview && frm.fields_dict.prescription_preview.$wrapper;
	if (!wrapper) return;

	const url = frm.doc.prescription_link;
	if (!url) {
		wrapper.html("");
		return;
	}

	const safe_url = frappe.utils.escape_html(url);
	wrapper.html(`
		<iframe
			src="${safe_url}"
			style="${CALL_LOG_A4_IFRAME_STYLE}"
		></iframe>
	`);
}

function render_medias_gallery(frm) {
	const wrapper = frm.fields_dict.medias_gallery && frm.fields_dict.medias_gallery.$wrapper;
	if (!wrapper) return;

	wrapper.html(`<div class="call-log-media-gallery">${render_media_groups(group_medias(frm.doc.medias || []))}</div>`);
}

function group_medias(rows) {
	const groups = {};
	rows.forEach((row) => {
		const type = row.type || "other";
		if (!groups[type]) groups[type] = [];
		groups[type].push(row);
	});
	return groups;
}

// Side-by-side columns (e.g. Prescription | Lab Report) rather than stacked full-width
// blocks -- wraps to a new row if there are ever more than 2 groups at once.
function render_media_groups(groups) {
	const types = Object.keys(groups);
	if (!types.length) {
		return `<div class="text-muted">${__("No medical documents uploaded.")}</div>`;
	}
	const columns = types.map((type) => render_media_group(type, groups[type])).join("");
	return `
		<div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px;">
			${columns}
		</div>
	`;
}

function render_media_group(type, rows) {
	return `
		<div>
			<div style="font-weight: 600; margin-bottom: 8px; font-size: 12px; color: var(--text-muted);">
				${frappe.utils.escape_html(CALL_LOG_MEDIA_TYPE_LABELS[type] || type)}
			</div>
			<div style="display: flex; flex-wrap: wrap; gap: 12px;">
				${rows.map(render_media_card).join("")}
			</div>
		</div>
	`;
}

// Opens a document in a dialog on the same page instead of a new browser tab, with a
// type label (Prescription/Lab Report) and Prev/Next to step through every document on
// this call log without closing and reopening the dialog. `medias` is the call log's full
// list (frm.doc.medias, in stored order); `start_index` is which one was clicked.
function call_log_open_media_preview(medias, start_index) {
	const dialog = new frappe.ui.Dialog({ size: "extra-large" });
	dialog.show();
	// frappe.ui.Dialog's `size` presets (small/large/extra-large) cap out well short of
	// full screen -- override the modal's own box directly so it fills the viewport, since
	// that's what a document preview actually needs (no useful "large but not full" size
	// for a PDF/photo).
	dialog.$wrapper.find(".modal-dialog").css({ width: "96vw", maxWidth: "96vw", margin: "2vh auto" });
	dialog.$wrapper.find(".modal-content").css({ height: "96vh" });

	function render_at(index) {
		const row = medias[index];
		const url = row.file || "";
		const filename = url.split("/").pop() || __("document");
		const is_image = /\.(png|jpe?g|gif|webp)$/i.test(url);
		const safe_url = frappe.utils.escape_html(url);
		const type = row.type || "other";

		dialog.set_title(filename);
		dialog.$body.css({ height: "calc(96vh - 120px)", overflow: "auto", padding: 0 }).html(`
			<div style="display: flex; align-items: center; justify-content: space-between; padding: 12px 20px;">
				<span class="indicator-pill ${CALL_LOG_MEDIA_TYPE_INDICATOR[type] || "gray"}">
					${frappe.utils.escape_html(CALL_LOG_MEDIA_TYPE_LABELS[type] || type)}
				</span>
				<div>
					<button class="btn btn-default btn-sm call-log-media-prev" ${index === 0 ? "disabled" : ""}>
						&lsaquo; ${__("Prev")}
					</button>
					<button class="btn btn-default btn-sm call-log-media-next" ${index === medias.length - 1 ? "disabled" : ""}>
						${__("Next")} &rsaquo;
					</button>
				</div>
			</div>
			<div style="height: calc(100% - 52px); padding: 0 20px 20px;">
				${
					is_image
						? `<img src="${safe_url}" style="max-width: 100%; max-height: 100%; display: block; margin: 0 auto;" />`
						: `<iframe src="${safe_url}" style="width: 100%; height: 100%; border: none;"></iframe>`
				}
			</div>
		`);

		dialog.$body.find(".call-log-media-prev").on("click", () => render_at(index - 1));
		dialog.$body.find(".call-log-media-next").on("click", () => render_at(index + 1));
	}

	render_at(start_index);
}

// `row.idx` is the child table's own 1-based row position -- frm.doc.medias is ordered by
// it, so `idx - 1` is exactly the index call_log_open_media_preview needs into that array,
// with no separate bookkeeping to keep in sync.
function render_media_card(row) {
	const url = row.file || "";
	const filename = url.split("/").pop() || __("document");
	const is_image = /\.(png|jpe?g|gif|webp)$/i.test(url);
	const safe_url = frappe.utils.escape_html(url);
	const safe_filename = frappe.utils.escape_html(filename);
	const index = row.idx - 1;

	if (is_image) {
		return `
			<div
				class="call-log-media-card"
				data-index="${index}"
				style="cursor: pointer;"
				title="${safe_filename}"
			>
				<img
					src="${safe_url}"
					style="width: 96px; height: 96px; object-fit: cover; border-radius: 6px; border: 1px solid var(--border-color);"
				/>
			</div>
		`;
	}

	return `
		<div
			class="call-log-media-card"
			data-index="${index}"
			style="display: flex; flex-direction: column; align-items: center; width: 96px; cursor: pointer;"
		>
			<div
				style="width: 96px; height: 96px; border: 1px solid var(--border-color); border-radius: 6px;
					display: flex; align-items: center; justify-content: center;"
			>
				${frappe.utils.icon("file", "lg")}
			</div>
			<div
				style="font-size: 11px; margin-top: 4px; text-align: center; overflow: hidden;
					text-overflow: ellipsis; white-space: nowrap; width: 100%;"
			>
				${safe_filename}
			</div>
		</div>
	`;
}
