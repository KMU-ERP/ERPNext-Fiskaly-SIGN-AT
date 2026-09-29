from io import BytesIO

import pyqrcode
from markupsafe import Markup


def fiskaly_qr_svg(data: str, scale: int = 4, quiet_zone: int = 4):
	"""Render an RKSV QR code with the ISO-recommended four-module quiet zone.

	The white background and embedded viewBox keep the quiet zone intact in the
	PDF renderer and on narrow thermal printers. The bounds also prevent custom
	print formats from accidentally creating an unreadable code.
	"""

	if not data:
		return ""
	scale = min(max(int(scale or 4), 2), 12)
	quiet_zone = min(max(int(quiet_zone or 4), 4), 12)
	stream = BytesIO()
	pyqrcode.create(str(data), error="M", mode="binary").svg(
		stream,
		scale=scale,
		quiet_zone=quiet_zone,
		module_color="#000",
		background="#fff",
		xmldecl=False,
		svgclass="rksv-qr-svg",
		omithw=True,
	)
	return Markup(stream.getvalue().decode("utf-8"))
