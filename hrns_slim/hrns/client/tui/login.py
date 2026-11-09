"""Account login dialog: show the device code while the runtime polls GitHub."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from ..login import DeviceAuthorization, LoginResult, LoginTarget, login


class DeviceLoginDialog(ModalScreen[LoginResult | None]):
    """Wait for approval in place; Esc cancels and stops the runtime process."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, target: LoginTarget) -> None:
        super().__init__()
        self.target = target
        self._status = Static("Requesting a device code…", markup=False, classes="dialog-help")
        self._hint = Static("Esc cancel", markup=False, classes="dialog-hint")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"Sign in to {self.target.provider}", classes="dialog-title")
            yield Static(
                "Approve the code in your browser. This window closes when the provider "
                "answers, and the token is stored in your operating system keyring.",
                markup=False,
                classes="dialog-help",
            )
            yield self._status
            yield self._hint

    def on_mount(self) -> None:
        self.run_worker(self._sign_in(), group="login", exclusive=True, exit_on_error=False)

    def on_unmount(self) -> None:
        self.workers.cancel_group(self, "login")

    async def _sign_in(self) -> None:
        try:
            result = await login(self.target, on_authorization=self._show_code)
        except Exception as exc:  # noqa: BLE001 - the dialog is the error surface
            self._status.update(f"Login failed: {exc}")
            self._hint.update("Esc close")
            return
        self.dismiss(result)

    def _show_code(self, authorization: DeviceAuthorization) -> None:
        minutes = max(1, authorization.expires_in // 60)
        self._status.update(
            f"Open {authorization.verification_uri}\n"
            f"Enter this code: {authorization.user_code}\n\n"
            f"The code expires in about {minutes} minutes."
        )

    def action_cancel(self) -> None:
        self.dismiss(None)
