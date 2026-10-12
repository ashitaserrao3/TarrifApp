"""
Airline picker: for every route in the schedule, which airline is cheapest
and how much you save by picking it.

Each flight is priced by the TariffEngine (rate + surcharges + add-on) for a
shipment of `weight` kg, using the minimum charge and the "cheaper at a
higher weight break" rule. Per airline we keep its cheapest flight, rank
the airlines, and compare the winner with the runner-up.

Only flights priced with status OK compete. Airlines without a loaded
tariff, or flights that need checking, are listed per lane so you know the
comparison may be incomplete.
"""

from dataclasses import dataclass, field

from .linker import STATUS_OK, TariffEngine
from .queries import weekly_frequency

DEFAULT_WEIGHT = 100.0


@dataclass
class AirlineOption:
    airline: str
    best: object            # FlightRate of this airline's cheapest flight
    total: float            # ₹ for the shipment
    per_kg: float           # total / weight
    flights: int            # priced flights this airline has on the lane
    weekly: int             # weekly departures across those flights


@dataclass
class LanePick:
    origin: str
    dest: str
    weight: float
    options: list = field(default_factory=list)   # AirlineOption, cheapest first
    unpriced: dict = field(default_factory=dict)  # airline -> reason

    @property
    def winner(self):
        return self.options[0] if self.options else None

    @property
    def runner_up(self):
        return self.options[1] if len(self.options) > 1 else None

    @property
    def tie(self):
        return bool(self.runner_up) and abs(self.runner_up.total - self.winner.total) < 0.005

    @property
    def saving(self):
        """₹ saved on this shipment vs the next-cheapest airline (None if no competitor)."""
        if not self.runner_up:
            return None
        return self.runner_up.total - self.winner.total

    @property
    def saving_per_kg(self):
        s = self.saving
        return None if s is None else s / self.weight

    @property
    def saving_pct(self):
        s = self.saving
        return None if s is None or not self.runner_up.total else 100 * s / self.runner_up.total

    @property
    def verdict(self):
        if not self.winner:
            return "No priced option"
        if not self.runner_up:
            return "Only priced airline"
        if self.tie:
            return "Tie"
        return "Cheapest"


def _airline_name(eng, code):
    return (eng.airlines.get(code) or {}).get("Name") or code


def pick_lane(eng, origin, dest, flights, weight):
    lane = LanePick(origin, dest, weight)
    by_airline = {}
    for r in eng.price_all(flights):
        name = _airline_name(eng, r.flight["Airline"])
        total = r.total_cost(weight) if r.status == STATUS_OK else None
        if total is None:
            lane.unpriced.setdefault(name, r.status)
            continue
        by_airline.setdefault(name, []).append((total, r))
    for name, priced in by_airline.items():
        lane.unpriced.pop(name, None)  # the airline does compete; some flights just weren't priced
        priced.sort(key=lambda x: (x[0], x[1].flight["DepTime"] or ""))
        total, best = priced[0]
        lane.options.append(AirlineOption(
            airline=name, best=best, total=total, per_kg=total / weight, flights=len(priced),
            weekly=sum(weekly_frequency(r.flight) for _, r in priced)))
    lane.options.sort(key=lambda o: (o.total, o.airline))
    return lane


def recommend(conn, as_of=None, weight=DEFAULT_WEIGHT, origins=None):
    """-> (engine, [LanePick]) for every lane in the schedule, as of a date."""
    if not weight or weight <= 0:
        raise ValueError("Shipment weight must be more than 0 kg")
    eng = TariffEngine(conn, as_of)
    lanes = {}
    for f in conn.execute("SELECT * FROM Flights ORDER BY Origin, Destination, DepTime, FlightNo"):
        f = dict(f)
        if f["Origin"] == f["Destination"] or (origins and f["Origin"] not in origins):
            continue
        lanes.setdefault((f["Origin"], f["Destination"]), []).append(f)
    return eng, [pick_lane(eng, o, d, fl, weight) for (o, d), fl in lanes.items()]


def summary(picks):
    """Network roll-up: lanes won per airline and total saving on one shipment per lane."""
    wins, saving = {}, {}
    for p in picks:
        if p.winner and not p.tie:
            wins[p.winner.airline] = wins.get(p.winner.airline, 0) + 1
            saving[p.winner.airline] = saving.get(p.winner.airline, 0) + (p.saving or 0)
    contested = [p for p in picks if p.saving is not None]
    return {
        "lanes": len(picks),
        "priced": sum(1 for p in picks if p.winner),
        "contested": len(contested),
        "ties": sum(1 for p in picks if p.tie),
        "total_saving": sum(p.saving for p in contested),
        "wins": sorted(wins.items(), key=lambda kv: (-kv[1], kv[0])),
        "saving_by_airline": saving,
    }


EXPORT_HEADER = ["Origin", "Destination", "Weight kg", "Recommendation", "Verdict", "Flight", "Dep Time",
                 "Product", "Total ₹", "₹/kg", "Runner-up", "Runner-up ₹", "Saving ₹", "Saving ₹/kg",
                 "Saving %", "All options", "Not compared"]


def export_row(p):
    w, r = p.winner, p.runner_up

    def rnd(v, n=2):
        return "" if v is None else round(v, n)

    return [p.origin, p.dest, f"{p.weight:g}", w.airline if w else "", p.verdict,
            w.best.flight["FlightNo"] if w else "", w.best.flight["DepTime"] if w else "",
            w.best.product if w else "", rnd(w and w.total), rnd(w and w.per_kg),
            r.airline if r else "", rnd(r and r.total), rnd(p.saving), rnd(p.saving_per_kg),
            rnd(p.saving_pct, 1),
            " | ".join(f"{o.airline} {o.per_kg:.2f}/kg" for o in p.options),
            " | ".join(f"{a} ({s})" for a, s in sorted(p.unpriced.items()))]
