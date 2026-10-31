"""Sobriété énergétique : vérifications de l'appli, de l'IA locale et du serveur (sysfs simulé)."""
import html
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import energy
from app.config import settings


def _write(root, rel, value):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(value + "\n")


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    monkeypatch.setattr(energy, "SYSFS", str(tmp_path))
    return str(tmp_path)


def _by_key(checks):
    return {c.key: c for c in checks}


def test_server_settings_that_waste_energy(sysfs):
    for cpu in ("cpu0", "cpu1"):
        _write(sysfs, f"devices/system/cpu/{cpu}/cpufreq/scaling_governor", "performance")
        _write(sysfs, f"devices/system/cpu/{cpu}/cpufreq/energy_performance_preference", "performance")
    _write(sysfs, "class/drm/card0/device/vendor", "0x1002")
    _write(sysfs, "class/drm/card0/device/power_dpm_force_performance_level", "high")
    _write(sysfs, "module/pcie_aspm/parameters/policy", "default [performance] powersave powersupersave")
    _write(sysfs, "class/scsi_host/host0/link_power_management_policy", "max_performance")
    c = {k: v for k, v in ((x.key, x) for x in (energy.check_cpu(), energy.check_gpu(), energy.check_aspm(), energy.check_sata()))}
    assert all(x.status == "warn" for x in c.values()), {k: v.status for k, v in c.items()}
    assert "powersave" in c["cpu"].fix and "tmpfiles.d" in c["cpu"].fix
    assert "echo auto" in c["gpu"].fix and "pcie_aspm.policy=powersupersave" in c["aspm"].fix


def test_server_settings_in_order(sysfs):
    _write(sysfs, "devices/system/cpu/cpu0/cpufreq/scaling_governor", "powersave")
    _write(sysfs, "devices/system/cpu/cpu0/cpufreq/energy_performance_preference", "balance_power")
    _write(sysfs, "class/drm/card0/device/vendor", "0x1002")
    _write(sysfs, "class/drm/card0/device/power_dpm_force_performance_level", "auto")
    _write(sysfs, "module/pcie_aspm/parameters/policy", "default performance powersave [powersupersave]")
    _write(sysfs, "class/scsi_host/host0/link_power_management_policy", "med_power_with_dipm")
    for check in (energy.check_cpu(), energy.check_gpu(), energy.check_aspm(), energy.check_sata()):
        assert check.status == "ok", (check.key, check.detail)


def test_missing_information_is_not_a_problem(sysfs):
    assert energy.check_cpu().status == "unknown"
    assert energy.check_aspm().status == "unknown"
    assert energy.check_gpu() is None and energy.check_sata() is None  # pas de carte AMD ni de SATA visibles


def test_imap_frequency(monkeypatch):
    monkeypatch.setattr(settings, "imap_host", "imap.test")
    monkeypatch.setattr(settings, "imap_user", "moi@exemple.fr")
    monkeypatch.setattr(settings, "imap_password", "x")
    monkeypatch.setattr(settings, "imap_poll_minutes", 2)
    assert energy.check_imap().status == "warn"
    monkeypatch.setattr(settings, "imap_poll_minutes", 20)
    assert energy.check_imap().status == "ok"


def _ollama(models):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"models": models}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _model(vram_share, minutes):
    expires = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    return {"name": "qwen3:8b", "size": 6_000_000_000, "size_vram": int(6_000_000_000 * vram_share),
            "expires_at": expires, "details": {"parameter_size": "8.2B"}}


@pytest.mark.parametrize("share, minutes, device, keep", [
    (0, 4, "warn", "ok"), (1, 4, "ok", "ok"), (0.5, 4, "warn", "ok"), (1, 60 * 24 * 365, "ok", "warn"),
])
def test_where_the_model_runs(monkeypatch, share, minutes, device, keep):
    srv = _ollama([_model(share, minutes)])
    try:
        monkeypatch.setattr(settings, "llm_url", f"http://127.0.0.1:{srv.server_address[1]}")
        monkeypatch.setattr(settings, "llm_model", "qwen3:8b")
        c = _by_key(energy.check_llm())
        assert c["llm"].status == device and c["keep_alive"].status == keep
        if share == 0:
            assert "OLLAMA_VULKAN=1" in c["llm"].fix and "8,2 milliards de paramètres" in c["llm"].detail
    finally:
        srv.shutdown()


def test_no_model_loaded_offers_a_test(monkeypatch):
    srv = _ollama([])
    try:
        monkeypatch.setattr(settings, "llm_url", f"http://127.0.0.1:{srv.server_address[1]}")
        monkeypatch.setattr(settings, "llm_model", "qwen3:8b")
        (c,) = energy.check_llm()
        assert c.status == "unknown" and c.extra.get("test")
    finally:
        srv.shutdown()


def test_page(client, sysfs):
    _write(sysfs, "devices/system/cpu/cpu0/cpufreq/scaling_governor", "performance")
    page = html.unescape(client.get("/settings/energy").text)
    assert "Sobriété énergétique" in page and "Processeur : profil d'énergie" in page and "À améliorer" in page
    assert "/settings/energy" in client.get("/settings").text
