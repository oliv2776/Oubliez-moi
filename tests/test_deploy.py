"""Garde-fous de la chaîne de publication : version de Python testée, actions épinglées, mise à jour automatique."""
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _ci():
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


def test_ci_tests_the_python_version_of_the_image():
    """Quand Dependabot change la version de Python de l'image, la CI doit la tester aussi."""
    version = re.search(r"^FROM python:(\d+\.\d+)", (ROOT / "Dockerfile").read_text(), re.M).group(1)
    assert version in _ci()["jobs"]["tests"]["strategy"]["matrix"]["python-version"], \
        f"Ajoutez Python {version} (celui de l'image) à la matrice de tests de .github/workflows/ci.yml"


def test_actions_are_pinned_by_commit():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses and all(re.search(r"@[0-9a-f]{40}$", u) for u in uses), [u for u in uses if not re.search(r"@[0-9a-f]{40}$", u)]


def test_weekly_rebuild_without_cache_and_scan():
    ci = _ci()
    triggers = ci.get("on") or ci.get(True)
    assert triggers["schedule"][0]["cron"] and "workflow_dispatch" in triggers
    build = next(s for s in ci["jobs"]["docker"]["steps"] if s.get("id") == "build")["with"]
    assert build["pull"] is True and "schedule" in build["no-cache"]
    scan = ci["jobs"]["scan"]["steps"][0]["with"]
    assert scan["exit-code"] == "1" and scan["ignore-unfixed"] is True


def test_compose_uses_the_published_image():
    service = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["oubliez-moi"]
    assert service["image"] == "ghcr.io/oliv2776/oubliez-moi:latest" and service["build"] == "."


def test_update_scripts_are_valid_shell():
    for script in ("update.sh", "install-auto-update.sh"):
        path = ROOT / "deploy" / script
        assert path.stat().st_mode & 0o111, f"{script} doit être exécutable"
        subprocess.run(["sh", "-n", str(path)], check=True)
    update = (ROOT / "deploy/update.sh").read_text()
    assert "docker tag \"$before\" \"$image\"" in update   # retour à l'image précédente si la nouvelle échoue
