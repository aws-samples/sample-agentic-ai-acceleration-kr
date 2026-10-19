"""The standard Textual footer, populated reliably on first display."""
from textual.widgets import Footer


class InstallerFooter(Footer):
    def on_mount(self) -> None:
        super().on_mount()
        # Initial focus may be set before Footer subscribes to binding changes.
        self.call_after_refresh(self.bindings_changed, self.screen)
