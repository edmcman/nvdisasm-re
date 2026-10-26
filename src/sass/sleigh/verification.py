"""Constrained modifier combinations used by the case planner.

Masks index Cartesian tuples, but only tuples with a complete modeled legal extension
become requirements. Scheduling fields have already been removed by modifiers.
"""
import heapq
import itertools
import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class VerificationPolicy:
    default_strength: int | str = 3
    opcode_strength: dict = field(default_factory=dict)

    def __post_init__(self):
        for strength in (self.default_strength, *self.opcode_strength.values()):
            if strength != "exhaustive" and (type(strength) is not int or strength < 1):
                raise ValueError("verification strength must be a positive integer or 'exhaustive'")

    def strength(self, row):
        return self.opcode_strength.get(row["opcode"], self.default_strength)


POLICY = VerificationPolicy()
SUPPORTED = frozenset(("native", "primitive"))


def forms(row, statuses=None):
    """Complete legal forms in canonical order, without evaluating any references."""
    c = row["combinations"]
    names = sorted(c["labels"])
    free = [n for n in names if n not in c["decides"]]
    for entry in sorted(c["table"]):
        *labs, status = entry
        if statuses is not None and status not in statuses:
            continue
        fixed = dict(zip(c["decides"], labs))
        for values in itertools.product(*(sorted(c["labels"][n]) for n in free)):
            yield {**fixed, **dict(zip(free, values))}, status


class Requirements:
    def __init__(self, row, policy=POLICY, statuses=SUPPORTED):
        self.row, self.c = row, row["combinations"]
        self.statuses = frozenset(statuses)
        self.domains = {n: sorted(ls) for n, ls in sorted(self.c["labels"].items()) if len(ls) > 1}
        self.configured_strength = policy.strength(row)
        self.strength = len(self.domains) if self.configured_strength == "exhaustive" else min(
            self.configured_strength, len(self.domains))
        self.groups, offset = [], 0
        for names in itertools.combinations(self.domains, self.strength):
            strides = [math.prod(len(self.domains[n]) for n in names[i + 1:]) for i in range(len(names))]
            indices = [{v: j for j, v in enumerate(self.domains[n])} for n in names]
            self.groups.append((names, offset, strides, indices))
            offset += math.prod(len(self.domains[n]) for n in names)
        self.lookup = {tuple(e[:-1]): e[-1] for e in self.c["table"]}
        self.required = self.project_table(lambda s: s != "opaque")
        self.plannable = self.project_table(lambda s: s in self.statuses)
        # A complete form's interaction mask is the intersection of per-label
        # masks. Each mask leaves groups not mentioning its modifier unchanged.
        # This costs O(modifiers) integer operations per candidate, rather than
        # O(number of modifier triples) Python tuple construction.
        self.allowed = {n: {v: self.required for v in ls} for n, ls in self.domains.items()}
        for names, offset, strides, indices in self.groups:
            size = math.prod(len(self.domains[n]) for n in names)
            group = ((1 << size) - 1) << offset
            for i, name in enumerate(names):
                stride, width = strides[i], len(indices[i]) * strides[i]
                for value, index in indices[i].items():
                    match = 0
                    block = (1 << stride) - 1
                    for start in range(index * stride, size, width):
                        match |= block << (offset + start)
                    self.allowed[name][value] = (self.allowed[name][value] & ~group) | (match & self.required)

    def project_table(self, accepts):
        bits = 0
        rows = [e[:-1] for e in self.c["table"] if accepts(e[-1])]
        for names, offset, strides, indices in self.groups:
            positions = [(i, self.c["decides"].index(n)) for i, n in enumerate(names) if n in self.c["decides"]]
            # Legality may involve many fields; project the rows first, then
            # expand free fields once per distinct projection.
            for projection in {tuple(labs[j] for _, j in positions) for labs in rows}:
                fixed = {names[i]: value for (i, _), value in zip(positions, projection)}
                choices = [(fixed[n],) if n in fixed else self.domains[n] for n in names]
                for values in itertools.product(*choices):
                    bit = offset + sum(indices[i][v] * strides[i] for i, v in enumerate(values))
                    bits |= 1 << bit
        return bits

    def compatible(self, settings):
        """Known settings extend to a modeled legal form; absent settings are not defaults."""
        if any(n not in self.c["labels"] or v not in self.c["labels"][n] for n, v in settings.items() if v is not None):
            return False
        fixed = {n: v for n, v in settings.items() if v is not None}
        if set(self.c["decides"]) <= fixed.keys():
            return self.lookup.get(tuple(fixed[n] for n in self.c["decides"]), "opaque") != "opaque"
        return any(status != "opaque" and all(n not in fixed or fixed[n] == v
                   for n, v in zip(self.c["decides"], labs)) for *labs, status in self.c["table"])

    def mask(self, settings):
        known = {n: v for n, v in settings.items() if n in self.domains and v is not None}
        bits = self.required
        if len(known) != len(self.domains):
            present = 0
            for names, offset, _, _ in self.groups:
                if all(n in known for n in names):
                    present |= ((1 << math.prod(len(self.domains[n]) for n in names)) - 1) << offset
            bits &= present
        for name, value in known.items():
            bits &= self.allowed[name].get(value, 0)
        return bits


class Cover:
    """Lazy greedy set cover. Only accept() covers requirements; failed adapters don't."""
    def __init__(self, requirements):
        self.requirements = requirements
        self.uncovered = requirements.required
        names = sorted(requirements.c["labels"])
        self.names, self.heap = names, []
        seen = set()
        for settings, status in forms(requirements.row, requirements.statuses):
            mask = requirements.mask(settings)
            # Equal masks have equal interactions, but their operands may differ;
            # retain alternatives so adapter failures can try another extension.
            values = tuple(settings[n] for n in names)
            if values in seen:
                continue
            seen.add(values)
            if mask:
                self.heap.append((-mask.bit_count(), values, status, mask))
        heapq.heapify(self.heap)

    def next(self):
        while self.heap and self.uncovered:
            score, values, status, mask = heapq.heappop(self.heap)
            current = (mask & self.uncovered).bit_count()
            if not current:
                continue
            if -score != current:
                heapq.heappush(self.heap, (-current, values, status, mask))
                continue
            return dict(zip(self.names, values)), status, mask
        return None

    def accept(self, mask):
        self.uncovered &= ~mask
