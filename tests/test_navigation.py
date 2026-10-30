"""Navigation latérale : toutes les pages, regroupées, page courante signalée."""
import re

PAGES = {
    "/": "Tableau de bord", "/tasks": "À traiter", "/profiles": "Profils", "/requests": "Demandes",
    "/brokers": "Courtiers", "/mailboxes": "Boîtes mail", "/exposures": "Expositions",
    "/privacy-cases": "Dossiers RGPD", "/settings": "Réglages",
}


def test_sidebar_lists_every_page_by_group(client):
    html = client.get("/").text
    sidebar = html[html.index('<aside class="sidebar"'):html.index("</aside>")]
    for href, label in PAGES.items():
        assert f'href="{href}"' in sidebar and f"<span>{label}</span>" in sidebar, label
    groups = ["Effacement chez les courtiers", "Ménage des boîtes mail", "Autres droits RGPD"]
    positions = [sidebar.index(g) for g in groups]
    assert positions == sorted(positions)
    assert sidebar.index("Effacement chez les courtiers") < sidebar.index("<span>Profils</span>") < sidebar.index("Ménage des boîtes mail")


def test_current_page_is_marked(client):
    for href, label in PAGES.items():
        html = client.get(href).text
        current = re.findall(r'<a href="([^"]+)" aria-current="page">', html)
        assert current == [href], (href, current)


def test_mobile_menu_and_login_page(client):
    html = client.get("/").text
    assert 'aria-controls="sidebar"' in html and 'aria-expanded="false"' in html
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anonymous:
        assert '<aside class="sidebar"' not in anonymous.get("/login").text


def test_filter_pages_support_instant_search(client):
    """Chaque page filtrable marque ses résultats : le script les remplace pendant la frappe."""
    for url in ("/requests", "/brokers", "/privacy-cases", "/exposures", "/mailboxes", "/search?q=ab"):
        page = client.get(url).text
        assert "data-results" in page and "filter-submit" in page, url
    assert "form.filters, form.filters-live" in client.get("/").text
