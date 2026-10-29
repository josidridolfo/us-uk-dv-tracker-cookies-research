"""
Crawl stage: raw capture records for one site visit at one geolocation.

Container-side: `models.py` defines the RawTrial/RawRequest/RawCookie records
a crawl writes and the analysis stage reads back; it carries no browser
dependency itself. `probes/*.js` are pinned JavaScript probe payloads carried
over from the Playwright era; they have no caller yet and wait on an
OpenWPM ProbeCommand to invoke them.

The pre-crawl exit gate lives at `dv_tracking.egress`, outside this package.
It runs inside the crawl container too -- it must observe the same relay
egress the browser uses -- but depends on nothing beyond stdlib and
`dv_tracking.constants`, which is why it is kept off this package's import
path rather than folded in here.
"""
