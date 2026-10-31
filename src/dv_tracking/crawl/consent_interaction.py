"""
The consent-interaction seam between a visit's pre-consent and post-consent phases.

`ConsentInteractor` is the interface: detect a consent banner and, when the
active arm calls for a decision, apply it, in one bounded call. The command
that places this in the OpenWPM sequence (`openwpm_run.ConsentInteractionCommand`)
and the sidecar record it writes depend only on this interface, not on which
library backs it, so the engine can change without touching either.

`AutoconsentInteractor` is the current implementation, built on a pinned
release of DuckDuckGo's `autoconsent` library vendored in
`tracker_lists/autoconsent/` (see that directory's `PROVENANCE.md`). The
vendored Consent-O-Matic assets in `tracker_lists/consentomatic/` are a
separate, independent presence detector consumed by
`dv_tracking.classify.banner`; this module does not read them, and two
detectors disagreeing on the same page is itself calibration evidence, not
something to reconcile here.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from dv_tracking.constants import TRACKER_LISTS_DIR, ConsentAction

AUTOCONSENT_DIR: Path = TRACKER_LISTS_DIR / "autoconsent"
DEFAULT_AUTOCONSENT_ENGINE_PATH: Path = AUTOCONSENT_DIR / "autoconsent.esm.js"
DEFAULT_AUTOCONSENT_RULES_PATH: Path = AUTOCONSENT_DIR / "rules.json"
# Pinned npm version; see tracker_lists/autoconsent/PROVENANCE.md.
AUTOCONSENT_VERSION: str = "17.1.0"

DecisionStatus = Literal["not_attempted", "no_banner", "applied", "failed", "timeout", "instrument_error"]

# Retries inside the engine's own detection loop. Capped at 1, not tuned
# up for recall: findCmp() waits for a DOM mutation on every retry past
# the first (`domActions.waitForMutation('html')`, a 60s default timeout
# with no config flag to shorten or disable it -- confirmed empirically,
# the "no banner" case took the full 5s test timeout at detectRetries=5),
# so retries>1 risks a single detection call stalling for up to a minute
# on a page that never mutates. By the time this runs, the page has
# already sat through the arm's pre-dwell (>=10s), which is where a
# banner that loads asynchronously is expected to have already appeared;
# the hard bound on total interaction time is the caller-supplied
# timeout_s regardless, enforced in-page by the bootstrap script's own
# Promise.race.
DEFAULT_DETECT_RETRIES: int = 1
# Selenium's own execute_async_script timeout must exceed the in-page
# bound or it raises before the bootstrap's race ever resolves.
SCRIPT_TIMEOUT_MARGIN_S: float = 5.0


class ScriptRunner(Protocol):
    """The subset of a Selenium webdriver this module depends on."""

    def execute_async_script(self, script: str, *args: object) -> object: ...

    def set_script_timeout(self, time_to_wait: float) -> None: ...


@dataclass(frozen=True)
class ConsentInteractionResult:
    """Outcome of one bounded detect-and-(maybe)-apply call.

    `category_targets` is a placeholder column, always `None` for
    `AutoconsentInteractor`: autoconsent exposes an opt-out/opt-in decision
    per CMP, not a per-category toggle, so there is no finer-grained target
    to record. A future per-category-capable engine would populate it; if
    none ever does, the column should be dropped rather than left unused.
    """

    engine_name: str
    engine_version: str
    action_requested: ConsentAction
    decision_status: DecisionStatus
    banner_present: bool
    detected_cmps: tuple[str, ...]
    matched_popup_cmp: str | None
    messages: tuple[dict[str, object], ...]
    clicks: int
    duration_s: float
    clicked_at_utc: str | None
    category_targets: None
    error: str | None

    def to_json(self) -> dict[str, object]:
        """Project the result to the dict written to interactions.jsonl."""
        return asdict(self)


class ConsentInteractor(Protocol):
    """Finds a consent banner and, when asked, applies an arm's decision to it.

    `run()` must not raise for a runtime fault (a WebDriver error, a script
    exception, an unparseable result): it returns a
    `decision_status="instrument_error"` result instead, carrying the
    fault in `error`. That status is reserved for a call that never
    produced a real engine result and must never be confused with
    `"failed"`, which means the engine ran and reported its own decision
    as unsuccessful. `ConsentInteractionCommand` depends on this -- a
    result is always available to write to `interactions.jsonl` and to
    derive `boundary_utc` from, even when the interaction itself went
    wrong. The only exception `run()` may raise is `ValueError` for an
    `action` outside `supported_actions`, a precondition
    the exit-7 refusal gate (`_unsupported_condition_actions`) already screens
    for before any browser opens.
    """

    name: str
    version: str
    supported_actions: frozenset[ConsentAction]

    def run(self, webdriver: ScriptRunner, action: ConsentAction, timeout_s: float) -> ConsentInteractionResult: ...


# Matches the one trailing `export { ... };` statement esbuild emits for
# this build; there is nothing after it in the file, so truncating there
# is equivalent to dropping just that statement.
_TRAILING_EXPORT_BLOCK = re.compile(r"\nexport \{.*\Z", re.DOTALL)


def _strip_esm_export(source: str) -> str:
    """Drop the trailing ES-module `export {...}` statement, leaving the file on disk untouched."""
    stripped = _TRAILING_EXPORT_BLOCK.sub("", source)
    if stripped == source:
        raise ValueError("expected a trailing ES-module export block; vendored file may have changed")
    return stripped


def _epoch_ms_to_iso(epoch_ms: float) -> str:
    """Convert a JS `Date.now()`-style epoch-millisecond value to an ISO-8601 UTC string."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).isoformat()


# `arguments[0]` is execute_async_script's own completion callback; no
# other arguments are passed from Python, since the action, timeout, and
# full rule set are baked into the script text once per interactor call
# rather than marshalled through the WebDriver wire protocol per visit.
# The IIFE takes it as a named `callback` parameter rather than
# re-reading `arguments[0]` inside its own (zero-argument) body, which
# resolves to `undefined` there -- confirmed empirically under real
# Firefox/geckodriver, where every call ran out the clock as a timeout.
#
# Settling on `_start()`'s own return value is not reliable for a
# decision: upstream's `detectPopups()` calls `onFirstPopupAppears(cmp)`
# (which runs `doOptOut`/`doOptIn`) without awaiting or returning that
# promise, so `_start()` can resolve while the click is still in flight --
# confirmed empirically against the real engine, where `_start()` resolved
# with the engine's own lifecycle still at "runningOptOut" and no
# `optOutResult` message yet sent. `startPromise` is still the correct
# signal for "nothing to decide" (no popup found, or action is "none"):
# `detectPopup()` itself sets `state.detectedPopups` synchronously, before
# the unawaited callback even runs, so checking it right after `startPromise`
# resolves is safe. When a popup was found and a decision was requested,
# the settle signal instead waits for the terminal message `doOptOut`/
# `doOptIn` sends synchronously, in the same tick as the engine's own
# final state update, right before returning.
_BOOTSTRAP_TEMPLATE = """
return (function (callback) {
  const action = __ACTION__;
  const timeoutMs = __TIMEOUT_MS__;
  const messages = [];
  const TERMINAL_MESSAGE_TYPES = ["optOutResult", "optInResult", "autoconsentError"];
  let settleResolve;
  const settlePromise = new Promise((resolve) => { settleResolve = resolve; });
  const sendMessage = async (message) => {
    messages.push(message);
    if (TERMINAL_MESSAGE_TYPES.indexOf(message.type) !== -1) {
      settleResolve();
    }
  };
  const config = {
    isMainWorld: true,
    enabled: true,
    autoAction: action,
    disabledCmps: [],
    enablePrehide: false,
    enableCosmeticRules: true,
    enableGeneratedRules: true,
    detectRetries: __DETECT_RETRIES__,
    prehideTimeout: 2000,
    enableHeuristicDetection: false,
    enablePopupMutationObserver: false,
    visualTest: false,
    heuristicMode: "off",
    heuristicPopupSearchTimeout: 500,
    enableHeuristicHitTest: false,
    performanceLoggingEnabled: false,
    logs: {
      lifecycle: false, rulesteps: false, detectionsteps: false,
      evals: false, errors: true, messages: false, waits: false,
    },
  };
  let startResolve;
  const startPromise = new Promise((resolve) => { startResolve = resolve; });
  const engine = new AutoConsent(sendMessage, null, null);
  engine.start = () => { startResolve(engine._start()); };
  engine.initialize(config, { autoconsent: __AUTOCONSENT_RULES__.autoconsent });
  const settleSignal = startPromise.then(() => {
    const decisionInFlight = action !== null && engine.state.detectedPopups.length > 0;
    return decisionInFlight ? settlePromise : null;
  });
  Promise.race([
    settleSignal.then(() => ({ timedOut: false })),
    new Promise((resolve) => setTimeout(() => resolve({ timedOut: true }), timeoutMs)),
  ]).then((outcome) => {
    callback({ timedOut: outcome.timedOut, state: engine.state, messages: messages });
  });
})(arguments[0]);
"""

_ACTION_TO_AUTO_ACTION: dict[str, str] = {"none": "null", "reject": '"optOut"', "accept": '"optIn"'}


class AutoconsentInteractor:
    """
    Consent-banner detection and decision application via DuckDuckGo's autoconsent.

    Detection and decision share one bounded call because the engine
    itself is built that way: `AutoConsent._start()` always detects first
    and only acts when `config.autoAction` is set, so there is no separate
    round-trip that could see a different DOM than the one the decision
    was made against. `action="none"` runs the identical detection path
    and simply never sets `autoAction`, which is what keeps the three
    arms' pre-consent phases comparable.

    `category_targets` on every result is always `None`: autoconsent
    exposes an opt-out/opt-in decision per CMP, not a per-category toggle,
    so there is no finer-grained target to record.
    """

    name: str = "autoconsent"
    version: str = AUTOCONSENT_VERSION
    supported_actions: frozenset[ConsentAction] = frozenset({"none", "reject", "accept"})

    def __init__(
        self,
        engine_path: Path = DEFAULT_AUTOCONSENT_ENGINE_PATH,
        rules_path: Path = DEFAULT_AUTOCONSENT_RULES_PATH,
        detect_retries: int = DEFAULT_DETECT_RETRIES,
    ) -> None:
        engine_js = _strip_esm_export(engine_path.read_text(encoding="utf-8"))
        rules_json = rules_path.read_text(encoding="utf-8")
        self._prelude = f"{engine_js}\nconst __AUTOCONSENT_RULES__ = {rules_json};\n"
        self._detect_retries = detect_retries

    def _script_for(self, action: ConsentAction, timeout_s: float) -> str:
        """Render the full injected script for one `run()` call."""
        bootstrap = (
            _BOOTSTRAP_TEMPLATE.replace("__ACTION__", _ACTION_TO_AUTO_ACTION[action])
            .replace("__TIMEOUT_MS__", str(int(timeout_s * 1000)))
            .replace("__DETECT_RETRIES__", str(self._detect_retries))
        )
        return self._prelude + bootstrap

    def run(self, webdriver: ScriptRunner, action: ConsentAction, timeout_s: float) -> ConsentInteractionResult:
        """
        Detect a consent banner and, for `action` other than "none", apply its decision.

        Args:
            webdriver: The Selenium webdriver for the current browser tab.
            action: One of `supported_actions`.
            timeout_s: Hard bound on the whole call; a banner that never
                resolves ends the call with `decision_status="timeout"`
                rather than hanging the visit.
        Returns:
            result: The interaction outcome.
        Raises:
            ValueError: When `action` is not in `supported_actions`.
        """
        if action not in self.supported_actions:
            raise ValueError(f"unsupported action {action!r}; supported: {sorted(self.supported_actions)}")
        script = self._script_for(action, timeout_s)
        start = time.monotonic()
        try:
            webdriver.set_script_timeout(timeout_s + SCRIPT_TIMEOUT_MARGIN_S)
            raw = webdriver.execute_async_script(script)
        except Exception as error:  # noqa: BLE001 -- a script/WebDriver fault must still produce a row, not crash the visit
            return self._instrument_error(action, time.monotonic() - start, repr(error))
        return self._parse(action, raw, time.monotonic() - start)

    def _instrument_error(self, action: ConsentAction, duration_s: float, error: str) -> ConsentInteractionResult:
        """Build a `decision_status="instrument_error"` result for a call that never returned a real engine result.

        Covers a WebDriver/script exception and an unparseable script
        payload alike: both mean the harness lost track of what happened
        in-page, not that a banner declined to yield. Keeping this status
        separate from `"failed"` keeps an instrument fault from being
        read as a consent outcome.
        """
        return ConsentInteractionResult(
            engine_name=self.name,
            engine_version=self.version,
            action_requested=action,
            decision_status="instrument_error",
            banner_present=False,
            detected_cmps=(),
            matched_popup_cmp=None,
            messages=(),
            clicks=0,
            duration_s=duration_s,
            clicked_at_utc=None,
            category_targets=None,
            error=error,
        )

    def _parse(self, action: ConsentAction, raw: object, duration_s: float) -> ConsentInteractionResult:
        """Build a result from the bootstrap script's `{timedOut, state, messages}` payload."""
        if not isinstance(raw, dict):
            return self._instrument_error(action, duration_s, f"unexpected script result: {raw!r}")

        timed_out = bool(raw.get("timedOut"))
        state = raw.get("state") or {}
        messages = tuple(raw.get("messages") or ())
        detected_cmps = tuple(state.get("detectedCmps") or ())
        detected_popups = tuple(state.get("detectedPopups") or ())
        banner_present = bool(detected_popups)
        end_time_ms = state.get("endTime")
        clicked_at_utc = _epoch_ms_to_iso(end_time_ms) if end_time_ms else None

        if timed_out:
            decision_status: DecisionStatus = "timeout"
        elif action == "none":
            decision_status = "not_attempted"
        elif not banner_present:
            decision_status = "no_banner"
        else:
            decision_messages = [m for m in messages if m.get("type") in ("optOutResult", "optInResult")]
            succeeded = bool(decision_messages and decision_messages[-1].get("result"))
            decision_status = "applied" if succeeded else "failed"

        return ConsentInteractionResult(
            engine_name=self.name,
            engine_version=self.version,
            action_requested=action,
            decision_status=decision_status,
            banner_present=banner_present,
            detected_cmps=detected_cmps,
            matched_popup_cmp=detected_popups[0] if detected_popups else None,
            messages=messages,
            clicks=int(state.get("clicks") or 0),
            duration_s=duration_s,
            clicked_at_utc=clicked_at_utc if decision_status == "applied" else None,
            category_targets=None,
            error=None,
        )
