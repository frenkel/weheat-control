"""Unblocking re-sendable (non-boost) requests: automatic readback recovery and explicit release."""

import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientSession
from homeassistant.exceptions import HomeAssistantError
from test_weheat import config_entry

from custom_components.weheat_control.api import Installation, WeheatApi, WeheatError
from custom_components.weheat_control.button import ReleaseUnresolvedButton
from custom_components.weheat_control.button import async_setup_entry as setup_buttons
from custom_components.weheat_control.coordinator import WeheatCoordinator
from custom_components.weheat_control.sensor import RequestSensor

pytest_plugins = ["test_weheat"]


def uncertain(settings=None, **extra):
    return {
        "state": "submission_uncertain",
        "terminal": False,
        "board_id": "board-1",
        "settings": {"PerformanceModePreset": 3} if settings is None else settings,
        "created_at": int(time.time()) - 60,
        "error_code": "http_error",
        "http_status": 502,
        **extra,
    }


@pytest.mark.parametrize(
    "operation",
    [
        uncertain(),
        uncertain(state="pending", request_id="request-1"),
        uncertain({}, properties={"DhwComfortPreference": 2}),
    ],
)
async def test_matching_readback_releases_re_sendable_request(hass, operation):
    async with ClientSession() as session:
        api = WeheatApi(session)
        coordinator = WeheatCoordinator(hass, config_entry(), api)
        coordinator.operation = operation
        readback = {"performanceModePreset": 3, "dhwComfortPreference": 2}
        with (
            patch.object(api, "settings", new_callable=AsyncMock, return_value=readback),
            patch.object(api, "status", new_callable=AsyncMock, side_effect=WeheatError("down")),
            patch.object(api, "patch", new_callable=AsyncMock) as mutation,
        ):
            await coordinator._async_update_data()
            mutation.assert_not_awaited()
        assert coordinator.operation["state"] == "succeeded"
        assert coordinator.operation["terminal"] is True
        assert coordinator.operation["recovery"] == "readback_matched"
        assert coordinator.operation["error_code"] == "http_error"  # diagnostics retained
        restored = WeheatCoordinator(hass, coordinator.entry, api)
        await restored.async_load_operation()
        assert restored.operation["terminal"] is True


async def test_different_readback_keeps_blocking_until_explicit_release(hass):
    async with ClientSession() as session:
        api = WeheatApi(session)
        coordinator = WeheatCoordinator(hass, config_entry(), api)
        coordinator.operation = uncertain()
        with patch.object(api, "settings", new_callable=AsyncMock, return_value={"performanceModePreset": 1}):
            await coordinator._async_update_data()
        assert coordinator.operation["terminal"] is False
        with pytest.raises(HomeAssistantError, match="unresolved"):
            await coordinator.async_write({"PerformanceModePreset": 2})

        with patch.object(api, "patch", new_callable=AsyncMock) as mutation:
            await ReleaseUnresolvedButton(coordinator).async_press()
            mutation.assert_not_awaited()
        assert coordinator.operation["state"] == "released"
        assert coordinator.operation["terminal"] is True
        assert RequestSensor(coordinator).native_value == "released"
        assert RequestSensor(coordinator).extra_state_attributes["http_status"] == 502

        restored = WeheatCoordinator(hass, coordinator.entry, api)
        await restored.async_load_operation()
        assert restored.operation["terminal"] is True
        with (
            patch.object(api, "settings", new_callable=AsyncMock, return_value={"performanceModePreset": 1}),
            patch.object(
                api, "installations", new_callable=AsyncMock, return_value=[Installation("installation-1", "board-1")]
            ),
            patch.object(api, "patch", new_callable=AsyncMock, return_value="request-2") as mutation,
            patch.object(restored, "async_request_refresh", new_callable=AsyncMock),
        ):
            await restored.async_write({"PerformanceModePreset": 2})
            mutation.assert_awaited_once_with("board-1", {"PerformanceModePreset": 2})
        assert restored.operation["history"][-1]["recovery"] == "released_by_user"
        assert restored.operation["history"][-1]["settings"] == {"PerformanceModePreset": 3}


async def test_boosts_are_never_released_or_auto_recovered(hass):
    async with ClientSession() as session:
        api = WeheatApi(session)
        coordinator = WeheatCoordinator(hass, config_entry(), api)
        end = str(int(time.time()) + 1800)
        coordinator.operation = uncertain({"DhwBoostEndTime": end})
        with patch.object(api, "settings", new_callable=AsyncMock, return_value={"dhwBoostEndTime": end}):
            await coordinator._async_update_data()
        assert coordinator.operation["terminal"] is False
        with pytest.raises(HomeAssistantError, match="boost"):
            await coordinator.async_release_unresolved()
        assert coordinator.operation["terminal"] is False
        # A hand-edited journal cannot turn a boost into a released record either.
        coordinator.operation.update(state="released", recovery="released_by_user")
        await coordinator._save_operation()
        restored = WeheatCoordinator(hass, coordinator.entry, api)
        await restored.async_load_operation()
        assert restored.operation["terminal"] is False


async def test_status_failure_still_blocks_boost_polling(hass):
    async with ClientSession() as session:
        api = WeheatApi(session)
        coordinator = WeheatCoordinator(hass, config_entry(), api)
        coordinator.operation = uncertain({"DhwBoostEndTime": "1"}, state="pending", request_id="request-1")
        with patch.object(api, "status", new_callable=AsyncMock, side_effect=WeheatError("down")):
            with pytest.raises(Exception):
                await coordinator._async_update_data()


async def test_release_requires_an_unresolved_request_and_survives_storage_failure(hass):
    async with ClientSession() as session:
        coordinator = WeheatCoordinator(hass, config_entry(), WeheatApi(session))
        with pytest.raises(HomeAssistantError, match="no unresolved"):
            await coordinator.async_release_unresolved()
        coordinator.operation = {**uncertain(), "state": "succeeded", "terminal": True}
        with pytest.raises(HomeAssistantError, match="no unresolved"):
            await coordinator.async_release_unresolved()
        coordinator.operation = uncertain()
        with patch.object(coordinator._store, "async_save", side_effect=OSError("disk full")):
            with pytest.raises(HomeAssistantError):
                await coordinator.async_release_unresolved()
        assert coordinator.operation["terminal"] is False


async def test_release_button_is_set_up_and_always_usable():
    current = Mock()
    current.entry.data = {"installation_id": "installation-1"}
    current.last_update_success = False
    entry = Mock(runtime_data=current)
    add = Mock()
    await setup_buttons(Mock(), entry, add)
    buttons = [entity for call in add.call_args_list for entity in call.args[0]]
    release = next(entity for entity in buttons if isinstance(entity, ReleaseUnresolvedButton))
    assert release.available
    assert release.unique_id == "installation-1_release_unresolved_request"
