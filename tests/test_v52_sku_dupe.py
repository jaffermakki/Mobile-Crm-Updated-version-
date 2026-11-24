"""v52.1 — a SKU that is already used gives a clear message, not a server error (found by the whole-build end-to-end run)."""
import pytest

from app.models import Product


@pytest.fixture(autouse=True)
def _clean(db_session):
    def wipe():
        db_session.rollback()
        for p in db_session.query(Product).filter(Product.sku.like("V52SKU-%")).all():
            db_session.delete(p)
        db_session.commit()
    wipe()
    yield
    wipe()


def _add(c, sku, name="Thing"):
    return c.post("/products/add", data={"sku": sku, "name": name, "price": "5", "cost": "2", "stock": "3"}, follow_redirects=True)


def test_adding_a_product_with_a_used_sku_says_so_and_adds_nothing(owner_client, db_session):
    assert _add(owner_client, "V52SKU-A", "First").status_code == 200
    r = _add(owner_client, "V52SKU-A", "Second")
    assert r.status_code == 200
    assert "already used by" in r.text and "First" in r.text
    assert db_session.query(Product).filter(Product.sku == "V52SKU-A").count() == 1


def test_blank_sku_or_name_is_refused(owner_client, db_session):
    r = _add(owner_client, "   ", "Nameless SKU")
    assert "needs a SKU and a name" in r.text
    r = _add(owner_client, "V52SKU-B", "   ")
    assert "needs a SKU and a name" in r.text
    assert db_session.query(Product).filter(Product.sku == "V52SKU-B").count() == 0


def test_the_sku_is_trimmed(owner_client, db_session):
    _add(owner_client, "  V52SKU-C  ", "Padded")
    assert db_session.query(Product).filter(Product.sku == "V52SKU-C").count() == 1
    assert "already used" in _add(owner_client, "V52SKU-C", "Again").text


def test_editing_to_another_products_sku_is_refused_but_keeping_your_own_is_fine(owner_client, db_session):
    _add(owner_client, "V52SKU-D1", "One")
    _add(owner_client, "V52SKU-D2", "Two")
    two = db_session.query(Product).filter(Product.sku == "V52SKU-D2").one()
    base = {"name": "Two", "category": "", "subcategory": "", "variant_group": "", "price": "5", "cost": "2", "stock": "3"}
    r = owner_client.post(f"/products/{two.id}/edit", data={**base, "sku": "V52SKU-D1"}, follow_redirects=True)
    assert r.status_code == 200 and "already used by" in r.text
    db_session.expire_all()
    assert db_session.get(Product, two.id).sku == "V52SKU-D2"
    r = owner_client.post(f"/products/{two.id}/edit", data={**base, "sku": "V52SKU-D2", "price": "6"}, follow_redirects=True)
    db_session.expire_all()
    assert float(db_session.get(Product, two.id).price) == 6.0
    r = owner_client.post(f"/products/{two.id}/edit", data={**base, "sku": "V52SKU-D3"}, follow_redirects=True)
    db_session.expire_all()
    assert db_session.get(Product, two.id).sku == "V52SKU-D3"


def test_a_cashier_still_cannot_add_products(cashier_client):
    r = cashier_client.post("/products/add", data={"sku": "V52SKU-Z", "name": "x"}, follow_redirects=False)
    assert r.status_code == 403
