"""The bookings page and the order form compute demand and coverage once per call, not once per article
made of parts built on order (p365)."""

import pytest

from conftest import sm_of
from factories import make_article, make_assembled
from reprintbuddy.domain import assembled, workqueue


@pytest.mark.parametrize("url", ["/bookings", "/orders/new"])
def test_demand_and_coverage_once_per_page(ready_client, monkeypatch, url):
    with sm_of(ready_client).begin() as s:
        a, b, c = (make_article(s, name=n, stock=5).id for n in ("Segment", "Side part L", "Side part R"))
        for i in range(3):
            make_assembled(s, name=f"Hose {i}", sku=f"EX-H{i}", mode="order", base=[(a, 2)],
                           fields=[[("-L", [(b, 1)]), ("-R", [(c, 1)])]])
    calls = {"demand": 0, "coverage": 0}
    real_demand, real_coverage = assembled.assembly_demand, workqueue.coverage_all

    def demand(*args, **kwargs):
        calls["demand"] += 1
        return real_demand(*args, **kwargs)

    def coverage(*args, **kwargs):
        calls["coverage"] += 1
        return real_coverage(*args, **kwargs)

    monkeypatch.setattr(assembled, "assembly_demand", demand)
    monkeypatch.setattr(workqueue, "coverage_all", coverage)
    page = ready_client.get(url)
    assert page.status_code == 200 and "EX-H2-R" in page.text
    assert calls == {"demand": 1, "coverage": 1}
