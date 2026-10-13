"""Read-only playback state classification; normal UI recovery is handled by batch_runner.

Never synthesizes mouse activity, focus/visibility, playback progress, or platform completion.
"""
from __future__ import annotations


def pause_reason(state: dict) -> str:
    """A fail-closed decision from the observed document/video state."""
    if state.get('guard_visible'):
        return 'manual_verification'
    if state.get('visibility') == 'hidden' or state.get('focused') is False:
        return 'foreground_required'
    if state.get('error_code'):
        return 'media_error'
    return 'normal_pause'


def is_buffering(state: dict) -> bool:
    if state.get('paused') or state.get('ended'):
        return False
    if state.get('ready_state') is not None and state['ready_state'] < 3:
        return True
    return state.get('network_state') == 2 and state.get('buffered_ahead', 0) <= 0.2
