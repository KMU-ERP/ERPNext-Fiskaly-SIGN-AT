window.erpnext_fiskaly_sign_at = window.erpnext_fiskaly_sign_at || {};

erpnext_fiskaly_sign_at.render_role_help = function (wrapper, config) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: config.page_title,
		single_column: true,
	});

	const escape = frappe.utils.escape_html;
	const render_action = (action, css_class = "btn-default") => `
		<button class="btn ${css_class} btn-sm" data-role-help-route="${escape(action.key)}">
			${escape(action.label)}
		</button>`;
	const render_section = (section, index) => `
		<section class="role-help-section">
			<div class="role-help-section-number">${index + 1}</div>
			<div>
				<h3>${escape(section.title)}</h3>
				<p>${escape(section.description)}</p>
				<ul>${section.items.map((item) => `<li>${escape(item)}</li>`).join("")}</ul>
			</div>
		</section>`;

	const help = $(`
		<div class="role-help">
			<section class="role-help-hero">
				<div>
					<div class="role-help-eyebrow">${escape(config.eyebrow)}</div>
					<h2>${escape(config.title)}</h2>
					<p>${escape(config.intro)}</p>
				</div>
				<div class="role-help-actions">
					${config.actions
						.map((action, index) => render_action(action, index === 0 ? "btn-primary" : "btn-default"))
						.join("")}
				</div>
			</section>
			<div class="role-help-notice">
				<strong>${escape(__("Your workspace"))}:</strong>
				${escape(config.workspace_note)}
			</div>
			<main class="role-help-content">
				${config.sections.map(render_section).join("")}
			</main>
		</div>`).appendTo(page.main);

	help.on("click", "[data-role-help-route]", function () {
		const action = config.actions.find(
			(item) => item.key === $(this).attr("data-role-help-route"),
		);
		if (action) {
			frappe.set_route(...action.route);
		}
	});
};
