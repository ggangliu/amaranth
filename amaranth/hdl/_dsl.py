from collections import OrderedDict
from contextlib import contextmanager, _GeneratorContextManager
from functools import wraps
from enum import Enum
import warnings
import sys

from .._utils import flatten
from ..utils import bits_for
from .. import tracer
from ._ast import *
from ._ast import _StatementList, _LateBoundStatement, Property
from ._ir import *
from ._cd import *
from ._xfrm import *


__all__ = ["SyntaxError", "SyntaxWarning", "Module"]


class SyntaxError(Exception):
    pass


class SyntaxWarning(Warning):
    pass


class _ModuleBuilderProxy:
    def __init__(self, builder, depth):
        object.__setattr__(self, "_builder", builder)
        object.__setattr__(self, "_depth", depth)


class _ModuleBuilderDomain(_ModuleBuilderProxy):
    def __init__(self, builder, depth, domain):
        super().__init__(builder, depth)
        self._domain = domain

    def __iadd__(self, assigns):
        self._builder._add_statement(assigns, domain=self._domain, depth=self._depth)
        return self


class _ModuleBuilderDomains(_ModuleBuilderProxy):
    def __getattr__(self, name):
        if name == "submodules":
            warnings.warn("Using '<module>.d.{}' would add statements to clock domain {!r}; "
                          "did you mean <module>.{} instead?"
                          .format(name, name, name),
                          SyntaxWarning, stacklevel=2)
        return _ModuleBuilderDomain(self._builder, self._depth, name)

    def __getitem__(self, name):
        return self.__getattr__(name)

    def __setattr__(self, name, value):
        if name == "_depth":
            object.__setattr__(self, name, value)
        elif not isinstance(value, _ModuleBuilderDomain):
            raise AttributeError("Cannot assign 'd.{}' attribute; did you mean 'd.{} +='?"
                                 .format(name, name))

    def __setitem__(self, name, value):
        return self.__setattr__(name, value)


class _ModuleBuilderRoot:
    def __init__(self, builder, depth):
        self._builder = builder
        self.domain = self.d = _ModuleBuilderDomains(builder, depth)

    def __getattr__(self, name):
        if name in ("comb", "sync"):
            raise AttributeError("'{}' object has no attribute '{}'; did you mean 'd.{}'?"
                                 .format(type(self).__name__, name, name))
        raise AttributeError("'{}' object has no attribute '{}'"
                             .format(type(self).__name__, name))


class _ModuleBuilderSubmodules:
    def __init__(self, builder):
        object.__setattr__(self, "_builder", builder)

    def __iadd__(self, modules):
        src_loc = tracer.get_src_loc()
        for module in flatten([modules]):
            self._builder._add_submodule(module, src_loc=src_loc)
        return self

    def __setattr__(self, name, submodule):
        src_loc = tracer.get_src_loc()
        self._builder._add_submodule(submodule, name, src_loc=src_loc)

    def __setitem__(self, name, submodule):
        src_loc = tracer.get_src_loc()
        self._builder._add_submodule(submodule, name, src_loc=src_loc)

    def __getattr__(self, name):
        return self._builder._get_submodule(name)

    def __getitem__(self, name):
        return self.__getattr__(name)


class _ModuleBuilderDomainSet:
    def __init__(self, builder):
        object.__setattr__(self, "_builder", builder)

    def __iadd__(self, domains):
        for domain in flatten([domains]):
            if not isinstance(domain, ClockDomain):
                raise TypeError("Only clock domains may be added to `m.domains`, not {!r}"
                                .format(domain))
            self._builder._add_domain(domain)
        return self

    def __setattr__(self, name, domain):
        if not isinstance(domain, ClockDomain):
            raise TypeError("Only clock domains may be added to `m.domains`, not {!r}"
                            .format(domain))
        if domain.name != name:
            raise NameError("Clock domain name {!r} must match name in `m.domains.{} += ...` "
                            "syntax"
                            .format(domain.name, name))
        self._builder._add_domain(domain)


# It's not particularly clean to depend on an internal interface, but, unfortunately, __bool__
# must be defined on a class to be called during implicit conversion.
class _GuardedContextManager(_GeneratorContextManager):
    def __init__(self, keyword, func, args, kwds):
        self.keyword = keyword
        return super().__init__(func, args, kwds)

    def __bool__(self):
        raise SyntaxError("`if m.{kw}(...):` does not work; use `with m.{kw}(...)`"
                          .format(kw=self.keyword))


def _guardedcontextmanager(keyword):
    def decorator(func):
        @wraps(func)
        def helper(*args, **kwds):
            return _GuardedContextManager(keyword, func, args, kwds)
        return helper
    return decorator


class FSMNextStatement(_LateBoundStatement):
    def __init__(self, ctrl_data, state, *, src_loc_at=0):
        self.ctrl_data = ctrl_data
        self.state = state
        super().__init__(src_loc_at=1 + src_loc_at)

    def resolve(self):
        return self.ctrl_data["signal"].eq(self.ctrl_data["encoding"][self.state])


class FSM:
    def __init__(self, data):
        self._data    = data
        self.encoding = data["encoding"]
        self.decoding = data["decoding"]

    def ongoing(self, name):
        if name not in self.encoding:
            self.encoding[name] = len(self.encoding)
            fsm_name = self._data["name"]
            self._data["ongoing"][name] = Signal(name=f"{fsm_name}_ongoing_{name}")
        return self._data["ongoing"][name]


def resolve_statement(stmt):
    if isinstance(stmt, _LateBoundStatement):
        return resolve_statement(stmt.resolve())
    elif isinstance(stmt, Switch):
        return Switch(
            test=stmt.test,
            cases=OrderedDict(
                (patterns, resolve_statements(stmts))
                for patterns, stmts in stmt.cases.items()
            ),
            src_loc=stmt.src_loc,
            case_src_locs=stmt.case_src_locs,
        )
    elif isinstance(stmt, (Assign, Property)):
        return stmt
    else:
        assert False # :nocov:


def resolve_statements(stmts):
    return _StatementList(resolve_statement(stmt) for stmt in stmts)


class Module(_ModuleBuilderRoot, Elaboratable):
    @classmethod
    def __init_subclass__(cls):
        raise SyntaxError("Instead of inheriting from `Module`, inherit from `Elaboratable` "
                          "and return a `Module` from the `elaborate(self, platform)` method")

    def __init__(self):
        _ModuleBuilderRoot.__init__(self, self, depth=0)
        self.submodules    = _ModuleBuilderSubmodules(self)
        self.domains       = _ModuleBuilderDomainSet(self)

        self._statements   = {}
        self._ctrl_context = None
        self._ctrl_stack   = []
        self._top_comb_statements = _StatementList()

        self._driving      = SignalDict()
        self._named_submodules = {}
        self._anon_submodules  = []
        self._domains      = {}
        self._generated    = {}
        self._src_loc      = tracer.get_src_loc()

    def _check_context(self, construct, context):
        if self._ctrl_context != context:
            if self._ctrl_context is None:
                raise SyntaxError("{} is not permitted outside of {}"
                                  .format(construct, context))
            else:
                if self._ctrl_context == "Switch":
                    secondary_context = "Case"
                if self._ctrl_context == "FSM":
                    secondary_context = "State"
                raise SyntaxError("{} is not permitted directly inside of {}; it is permitted "
                                  "inside of {} {}"
                                  .format(construct, self._ctrl_context,
                                          self._ctrl_context, secondary_context))

    def _get_ctrl(self, name):
        if self._ctrl_stack:
            top_name, top_data = self._ctrl_stack[-1]
            if top_name == name:
                return top_data

    def _flush_ctrl(self):
        while len(self._ctrl_stack) > self.domain._depth:
            self._pop_ctrl()

    def _set_ctrl(self, name, data):
        self._flush_ctrl()
        self._ctrl_stack.append((name, data))
        return data

    def _check_signed_cond(self, cond):
        cond = Value.cast(cond)
        if sys.version_info < (3, 12, 0) and cond.shape().signed:
            # TODO(py3.11): remove; ~True is a warning in 3.12+, finally!
            warnings.warn("Signed values in If/Elif conditions usually result from inverting "
                          "Python booleans with ~, which leads to unexpected results. "
                          "Replace `~flag` with `not flag`. (If this is a false positive, "
                          "silence this warning with `m.If(x)` → `m.If(x.bool())`.)",
                          SyntaxWarning, stacklevel=4)
        return cond

    @_guardedcontextmanager("If")
    def If(self, cond):
        self._check_context("If", context=None)
        cond = self._check_signed_cond(cond)
        src_loc = tracer.get_src_loc(src_loc_at=1)
        if_data = self._set_ctrl("If", {
            "depth":    self.domain._depth,
            "tests":    [],
            "bodies":   [],
            "src_loc":  src_loc,
            "src_locs": [],
        })
        try:
            _outer_case, self._statements = self._statements, {}
            self.domain._depth += 1
            yield
            self._flush_ctrl()
            if_data["tests"].append(cond)
            if_data["bodies"].append(self._statements)
            if_data["src_locs"].append(src_loc)
        finally:
            self.domain._depth -= 1
            self._statements = _outer_case

    @_guardedcontextmanager("Elif")
    def Elif(self, cond):
        self._check_context("Elif", context=None)
        cond = self._check_signed_cond(cond)
        src_loc = tracer.get_src_loc(src_loc_at=1)
        if_data = self._get_ctrl("If")
        if if_data is None or if_data["depth"] != self.domain._depth:
            raise SyntaxError("Elif without preceding If")
        try:
            _outer_case, self._statements = self._statements, {}
            self.domain._depth += 1
            yield
            self._flush_ctrl()
            if_data["tests"].append(cond)
            if_data["bodies"].append(self._statements)
            if_data["src_locs"].append(src_loc)
        finally:
            self.domain._depth -= 1
            self._statements = _outer_case

    @_guardedcontextmanager("Else")
    def Else(self):
        self._check_context("Else", context=None)
        src_loc = tracer.get_src_loc(src_loc_at=1)
        if_data = self._get_ctrl("If")
        if if_data is None or if_data["depth"] != self.domain._depth:
            raise SyntaxError("Else without preceding If/Elif")
        try:
            _outer_case, self._statements = self._statements, {}
            self.domain._depth += 1
            yield
            self._flush_ctrl()
            if_data["bodies"].append(self._statements)
            if_data["src_locs"].append(src_loc)
        finally:
            self.domain._depth -= 1
            self._statements = _outer_case
        self._pop_ctrl()

    @contextmanager
    def Switch(self, test):
        self._check_context("Switch", context=None)
        switch_data = self._set_ctrl("Switch", {
            "test":    Value.cast(test),
            "cases":   OrderedDict(),
            "src_loc": tracer.get_src_loc(src_loc_at=1),
            "case_src_locs": {},
        })
        try:
            self._ctrl_context = "Switch"
            self.domain._depth += 1
            yield
        finally:
            self.domain._depth -= 1
            self._ctrl_context = None
        self._pop_ctrl()

    @contextmanager
    def Case(self, *patterns):
        self._check_context("Case", context="Switch")
        src_loc = tracer.get_src_loc(src_loc_at=1)
        switch_data = self._get_ctrl("Switch")
        new_patterns = ()
        if () in switch_data["cases"]:
            warnings.warn("A case defined after the default case will never be active",
                          SyntaxWarning, stacklevel=3)
        # This code should accept exactly the same patterns as `v.matches(...)`.
        for pattern in patterns:
            if isinstance(pattern, str) and any(bit not in "01- \t" for bit in pattern):
                raise SyntaxError("Case pattern '{}' must consist of 0, 1, and - (don't care) "
                                  "bits, and may include whitespace"
                                  .format(pattern))
            if (isinstance(pattern, str) and
                    len("".join(pattern.split())) != len(switch_data["test"])):
                raise SyntaxError("Case pattern '{}' must have the same width as switch value "
                                  "(which is {})"
                                  .format(pattern, len(switch_data["test"])))
            if isinstance(pattern, str):
                new_patterns = (*new_patterns, pattern)
            else:
                try:
                    orig_pattern, pattern = pattern, Const.cast(pattern)
                except TypeError as e:
                    raise SyntaxError("Case pattern must be a string or a constant-castable "
                                      "expression, not {!r}"
                                      .format(pattern)) from e
                pattern_len = bits_for(pattern.value)
                if pattern.value == 0:
                    pattern_len = 0
                if pattern_len > len(switch_data["test"]):
                    warnings.warn("Case pattern '{!r}' ({}'{:b}) is wider than switch value "
                                  "(which has width {}); comparison will never be true"
                                  .format(orig_pattern, pattern_len, pattern.value,
                                          len(switch_data["test"])),
                                  SyntaxWarning, stacklevel=3)
                    continue
                new_patterns = (*new_patterns, pattern.value)
        try:
            _outer_case, self._statements = self._statements, {}
            self._ctrl_context = None
            yield
            self._flush_ctrl()
            # If none of the provided cases can possibly be true, omit this branch completely.
            # Likewise, omit this branch if another branch with this exact set of patterns already
            # exists (since otherwise we'd overwrite the previous branch's slot in the dict).
            if new_patterns and new_patterns not in switch_data["cases"]:
                switch_data["cases"][new_patterns] = self._statements
                switch_data["case_src_locs"][new_patterns] = src_loc
        finally:
            self._ctrl_context = "Switch"
            self._statements = _outer_case

    @contextmanager
    def Default(self):
        self._check_context("Default", context="Switch")
        src_loc = tracer.get_src_loc(src_loc_at=1)
        switch_data = self._get_ctrl("Switch")
        if () in switch_data["cases"]:
            warnings.warn("A case defined after the default case will never be active",
                          SyntaxWarning, stacklevel=3)
        try:
            _outer_case, self._statements = self._statements, {}
            self._ctrl_context = None
            yield
            self._flush_ctrl()
            if () not in switch_data["cases"]:
                switch_data["cases"][()] = self._statements
                switch_data["case_src_locs"][()] = src_loc
        finally:
            self._ctrl_context = "Switch"
            self._statements = _outer_case

    @contextmanager
    def FSM(self, init=None, domain="sync", name="fsm", *, reset=None):
        self._check_context("FSM", context=None)
        if domain == "comb":
            raise ValueError(f"FSM may not be driven by the '{domain}' domain")
        # TODO(amaranth-0.7): remove
        if reset is not None:
            if init is not None:
                raise ValueError("Cannot specify both `reset` and `init`")
            warnings.warn("`reset=` is deprecated, use `init=` instead",
                          DeprecationWarning, stacklevel=2)
            init = reset
        fsm_data = self._set_ctrl("FSM", {
            "name":     name,
            "init":     init,
            "domain":   domain,
            "encoding": OrderedDict(),
            "decoding": OrderedDict(),
            "ongoing":  {},
            "states":   OrderedDict(),
            "src_loc":  tracer.get_src_loc(src_loc_at=1),
            "state_src_locs": {},
        })
        self._generated[name] = fsm = FSM(fsm_data)
        try:
            self._ctrl_context = "FSM"
            self.domain._depth += 1
            yield fsm
            for state_name in fsm_data["encoding"]:
                if state_name not in fsm_data["states"]:
                    raise NameError("FSM state '{}' is referenced but not defined"
                                    .format(state_name))
        finally:
            self.domain._depth -= 1
            self._ctrl_context = None
        self._pop_ctrl()
        fsm.state = fsm_data["signal"]

    @contextmanager
    def State(self, name):
        self._check_context("FSM State", context="FSM")
        src_loc = tracer.get_src_loc(src_loc_at=1)
        fsm_data = self._get_ctrl("FSM")
        if name in fsm_data["states"]:
            raise NameError(f"FSM state '{name}' is already defined")
        if name not in fsm_data["encoding"]:
            fsm_name = fsm_data["name"]
            fsm_data["encoding"][name] = len(fsm_data["encoding"])
            fsm_data["ongoing"][name] = Signal(name=f"{fsm_name}_ongoing_{name}")
        try:
            _outer_case, self._statements = self._statements, {}
            self._ctrl_context = None
            yield
            self._flush_ctrl()
            fsm_data["states"][name] = self._statements
            fsm_data["state_src_locs"][name] = src_loc
        finally:
            self._ctrl_context = "FSM"
            self._statements = _outer_case

    @property
    def next(self):
        raise SyntaxError("Only assignment to `m.next` is permitted")

    @next.setter
    def next(self, name):
        if self._ctrl_context != "FSM":
            for level, (ctrl_name, ctrl_data) in enumerate(reversed(self._ctrl_stack)):
                if ctrl_name == "FSM":
                    if name not in ctrl_data["encoding"]:
                        fsm_name = ctrl_data["name"]
                        ctrl_data["encoding"][name] = len(ctrl_data["encoding"])
                        ctrl_data["ongoing"][name] = Signal(name=f"{fsm_name}_ongoing_{name}")
                    self._add_statement(
                        assigns=[FSMNextStatement(ctrl_data, name)],
                        domain=ctrl_data["domain"],
                        depth=len(self._ctrl_stack))
                    return

        raise SyntaxError("`m.next = <...>` is only permitted inside an FSM state")

    def _pop_ctrl(self):
        name, data = self._ctrl_stack.pop()
        src_loc = data["src_loc"]

        if name == "If":
            if_tests, if_bodies = data["tests"], data["bodies"]
            if_src_locs = data["src_locs"]

            # Use dict to ensure deterministic iteration.
            domains = {}
            for if_case in if_bodies:
                for domain in if_case:
                    domains[domain] = None

            for domain in domains:
                tests, cases = [], OrderedDict()
                for if_test, if_case in zip(if_tests + [None], if_bodies):
                    if if_test is not None:
                        if len(if_test) != 1:
                            if_test = if_test.bool()
                        tests.append(if_test)

                    if if_test is not None:
                        match = ("1" + "-" * (len(tests) - 1)).rjust(len(if_tests), "-")
                    else:
                        match = None
                    cases[match] = if_case.get(domain, [])

                self._statements.setdefault(domain, []).append(Switch(Cat(tests), cases,
                    src_loc=src_loc, case_src_locs=dict(zip(cases, if_src_locs))))

        if name == "Switch":
            switch_test, switch_cases = data["test"], data["cases"]
            switch_case_src_locs = data["case_src_locs"]

            domains = {}
            for stmts in switch_cases.values():
                for domain in stmts:
                    domains[domain] = None

            for domain in domains:
                domain_cases = OrderedDict()
                for patterns, stmts in switch_cases.items():
                    domain_cases[patterns] = stmts.get(domain, [])

                self._statements.setdefault(domain, []).append(Switch(switch_test, domain_cases,
                    src_loc=src_loc, case_src_locs=switch_case_src_locs))

        if name == "FSM":
            fsm_name, fsm_init, fsm_encoding, fsm_decoding, fsm_states, fsm_ongoing = \
                data["name"], data["init"], data["encoding"], data["decoding"], data["states"], data["ongoing"]
            fsm_state_src_locs = data["state_src_locs"]
            if not fsm_states:
                data["signal"] = Signal(0, name=f"{fsm_name}_state", src_loc_at=2)
                return
            if fsm_init is None:
                init = fsm_encoding[next(iter(fsm_states))]
            else:
                init = fsm_encoding[fsm_init]
            # The FSM is encoded such that the state with encoding 0 is always the init state.
            fsm_decoding.update((n, s) for s, n in fsm_encoding.items())
            data["signal"] = fsm_signal = Signal(range(len(fsm_encoding)), init=init,
                                                 name=f"{fsm_name}_state", src_loc_at=2,
                                                 decoder=lambda n: f"{fsm_decoding[n]}/{n}")

            for name, sig in fsm_ongoing.items():
                self._top_comb_statements.append(
                    sig.eq(Operator("==", [fsm_signal, fsm_encoding[name]], src_loc_at=0)))

            domains = {}
            for stmts in fsm_states.values():
                for domain in stmts:
                    domains[domain] = None

            for domain in domains:
                domain_states = OrderedDict()
                for state, stmts in fsm_states.items():
                    domain_states[state] = stmts.get(domain, [])

                self._statements.setdefault(domain, []).append(Switch(fsm_signal,
                    OrderedDict((fsm_encoding[name], stmts) for name, stmts in domain_states.items()),
                    src_loc=src_loc, case_src_locs={fsm_encoding[name]: fsm_state_src_locs[name]
                                                    for name in fsm_states}))

    def _add_statement(self, assigns, domain, depth):
        while len(self._ctrl_stack) > self.domain._depth:
            self._pop_ctrl()

        for stmt in Statement.cast(assigns):
            if not isinstance(stmt, (Assign, Property, _LateBoundStatement)):
                raise SyntaxError(
                    f"Only assignments and property checks may be appended to d.{domain}")

            stmt._MustUse__used = True

            if isinstance(stmt, Assign):
                for signal in stmt._lhs_signals():
                    if signal not in self._driving:
                        self._driving[signal] = domain
                    elif self._driving[signal] != domain:
                        cd_curr = self._driving[signal]
                        raise SyntaxError(
                            f"Driver-driver conflict: trying to drive {signal!r} from d.{domain}, but it is "
                            f"already driven from d.{cd_curr}")

            self._statements.setdefault(domain, []).append(stmt)

    def _add_submodule(self, submodule, name=None, src_loc=None):
        if not hasattr(submodule, "elaborate"):
            raise TypeError("Trying to add {!r}, which does not implement .elaborate(), as "
                            "a submodule".format(submodule))
        if name == None:
            self._anon_submodules.append((submodule, src_loc))
        else:
            if name in self._named_submodules:
                raise NameError(f"Submodule named '{name}' already exists")
            self._named_submodules[name] = (submodule, src_loc)

    def _get_submodule(self, name):
        if name in self._named_submodules:
            submodule, _src_loc = self._named_submodules[name]
            return submodule
        else:
            raise AttributeError(f"No submodule named '{name}' exists")

    def _add_domain(self, cd):
        if cd.name in self._domains:
            raise NameError(f"Clock domain named '{cd.name}' already exists")
        self._domains[cd.name] = cd

    def _flush(self):
        while self._ctrl_stack:
            self._pop_ctrl()

    def elaborate(self, platform):
        self._flush()

        fragment = Fragment(src_loc=self._src_loc)
        for name, (submodule, src_loc) in self._named_submodules.items():
            fragment.add_subfragment(Fragment.get(submodule, platform), name, src_loc=src_loc)
        for submodule, src_loc in self._anon_submodules:
            fragment.add_subfragment(Fragment.get(submodule, platform), None, src_loc=src_loc)
        for domain, statements in self._statements.items():
            statements = resolve_statements(statements)
            fragment.add_statements(domain, statements)
            for signal in statements._lhs_signals():
                fragment.add_driver(signal, domain)
        fragment.add_statements("comb", self._top_comb_statements)
        for signal in self._top_comb_statements._lhs_signals():
            fragment.add_driver(signal, "comb")
        fragment.add_domains(self._domains.values())
        fragment.generated.update(self._generated)
        return fragment
