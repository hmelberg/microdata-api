# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Translate a microdata script into a runnable Python program.

The microdata ``MicroParser`` turns each line into an instruction dict (the IR);
this module walks that IR and emits thin calls to the runtime ops:

    backend="pandas" -> m2py_runtime.pandas_ops on an eager pd.DataFrame
    backend="polars" -> m2py_runtime.polars_ops on a lazy pl.LazyFrame
                        (collected with the streaming engine at the end)

The emitted program is standalone (given the runtime package on PYTHONPATH) and
is the artifact you can run offline — e.g. send the string to a worker / API and
execute it natively where polars' streaming engine and real files exist.

Unsupported verbs (or, for polars, expressions the compiler can't map) are
emitted as ``# UNTRANSLATED:`` comments — never silently-wrong code. Call
:func:`unsupported` to list them for a script without generating.
"""

import re

import m2py
from m2py_runtime.exprcompile import compile_expr, UnsupportedExpr
from m2py_runtime.keys import resolve_merge_key, key_col_from_cols
from m2py_runtime.manifest import _format_from

# Extensions that mark a `require` source as a concrete file/URL dataset (vs a
# registry id like "no.ssb.fdb:43"). DuckDB/SQL sources are a follow-on.
_SOURCE_EXTS = (".csv", ".parquet")


def _looks_like_source(s):
    return isinstance(s, str) and s.lower().endswith(_SOURCE_EXTS)


# prediction verbs (transform: fit a model and add predicted/residual columns).
# poisson-predict is NOT a real microdata command (the emulator rejects it).
PREDICT = {
    "regress-predict": "regress_predict", "logit-predict": "logit_predict",
    "probit-predict": "probit_predict", "mlogit-predict": "mlogit_predict",
    "negative-binomial-predict": "negative_binomial_predict",
}
# binary/multinomial predicts: `predicted` is Xβ, `probabilities` is P(Y=…)
PREDICT_BINARY = {"logit-predict", "probit-predict", "mlogit-predict"}
# TRANSFORM verbs reassign the working frame (df / lf -> new frame).
TRANSFORM = {
    "generate", "replace", "recode", "keep", "drop", "rename", "destring",
    "collapse", "aggregate", "merge", "reshape-to-panel", "reshape-from-panel",
    "clone-variables", "ivregress-predict", "regress-panel-predict",
} | set(PREDICT)
# ANALYSIS verbs compute a side result and PRINT it; the working frame is
# unchanged (matching the emulator, where summarize/tabulate/regress don't alter
# the active dataset).
# regression family -> op name (analysis verbs returning a coefficient table)
REGRESSION = {
    "regress": "regress", "logit": "logit", "probit": "probit",
    "poisson": "poisson", "negative-binomial": "negative_binomial",
}
# survival verbs -> op name (analysis verbs, lifelines)
SURVIVAL = {"cox": "cox", "kaplan-meier": "kaplan_meier", "weibull": "weibull"}
# panel & IV regression (analysis verbs, linearmodels/statsmodels)
PANEL_IV = {"regress-panel", "regress-panel-diff", "ivregress"}
ANALYSIS = ({"summarize", "tabulate", "correlate", "mlogit", "rdd",
             "normaltest", "ci", "anova", "hausman",
             "summarize-panel", "tabulate-panel", "transitions-panel"}
            | set(REGRESSION) | set(SURVIVAL) | PANEL_IV)
# PLOT verbs build a plotly Figure (terminal, like analysis). Offline they are
# written to an HTML file; in-memory (tests) the figure object is left in scope.
PLOT = {"histogram", "barchart", "scatter", "boxplot",
        "piechart", "hexbin", "sankey", "coefplot"}

# dataset/session verbs — handled by the translate loop (they switch the active
# dataset / create variables), not by the per-frame emitters.
SESSION = {"create-dataset", "use", "clone-dataset", "delete-dataset",
           "rename-dataset", "clone-units"}
# label verbs are display-only in the emulator (the data keeps its codes), so
# they are no-ops on the offline data — recorded as comments, not flagged.
LABELS = {"define-labels", "assign-labels", "drop-labels", "list-labels"}

SUPPORTED = TRANSFORM | ANALYSIS | PLOT | SESSION | LABELS

# Options each verb actually honours. Any option on a line that is NOT listed
# here makes the line UNTRANSLATED — so an unrecognised flag (e.g. a tabulate
# formatting option) is surfaced, never silently dropped. Keep these in sync
# with what _emit / _emit_analysis and the runtime ops implement.
HANDLED_OPTIONS = {
    "generate": set(), "replace": set(), "recode": set(), "rename": set(),
    "keep": set(), "drop": set(),
    "destring": {"force"},                 # always coerces == force semantics
    "clone-variables": {"prefix", "suffix"},
    "reshape-to-panel": set(),
    "reshape-from-panel": set(),
    "collapse": {"by"}, "aggregate": {"by"},
    "merge": {"on", "outer_join"},
    "summarize": {"by", "gini", "iqr"},
    # two-way is via args, not an option; freq just shows counts (always on)
    "tabulate": {"by", "missing", "freq", "chi2", "top", "bottom",
                 "cellpct", "rowpct", "colpct", "cell", "row", "col"},
    "correlate": {"pairwise", "covariance"},   # sig/obs (text/extra cols) deferred
    "normaltest": set(),
    "ci": {"level"},
    "summarize-panel": {"gini", "iqr"},
    # tabulate-panel: tid is the columns; summarize()-volume variant deferred
    "tabulate-panel": {"missing", "rowpct", "colpct", "row", "col"},
    "transitions-panel": set(),
    "anova": set(),
    "hausman": set(),
    # regression family: noconstant only; or/irr/robust/exposure/level deferred
    "regress": {"noconstant"},
    "logit": {"noconstant"},
    "probit": {"noconstant"},
    "poisson": {"noconstant"},
    "negative-binomial": {"noconstant"},
    # panel: effect selectors; robust/level/cluster deferred
    "regress-panel": {"fe", "re", "random", "be", "pooled"},
    # IV: only 2SLS implemented; liml/gmm/robust/level deferred
    "ivregress": {"tsls", "2sls"},
    "ivregress-predict": {"predicted", "residuals", "tsls", "2sls"},
    "regress-panel-diff": {"pooled"},
    "regress-panel-predict": {"fe", "re", "random", "be", "pooled",
                              "predicted", "residuals", "effects"},
    "mlogit": {"noconstant"},
    # rdd: local-polynomial OLS (sharp + fuzzy); cluster/robust/derivate deferred
    "rdd": {"cutoff", "polynomial", "fuzzy"},
    # survival: by/level/hazard variants deferred
    "cox": set(),
    "kaplan-meier": set(),
    "weibull": set(),
    # plots
    "histogram": {"bin", "nbins", "discrete", "percent", "density", "freq", "normal"},
    # statistic via parenthesised (stat), not a flag
    "barchart": {"over", "horizontal", "stack"},
    "scatter": {"by", "color"},
    "boxplot": {"over"},
    "piechart": set(),                  # (percent) via parenthesised stat
    "hexbin": {"bin", "nbins"},
    "sankey": set(),
    "coefplot": {"standardize", "noconstant"},
    # predict variants (transform): name the predicted/residual columns
    # (binary outcomes also accept `probabilities`; regress `cooksd` deferred)
    **{v: ({"predicted", "residuals", "probabilities", "noconstant"}
           if v in PREDICT_BINARY else {"predicted", "residuals", "noconstant"})
       for v in PREDICT},
}


def _unhandled_options(instr):
    """Return option names present on this instruction that the translator does
    not honour (so the caller can mark the line UNTRANSLATED)."""
    cmd = instr["command"]
    opts = instr.get("options") or {}
    return sorted(set(opts) - HANDLED_OPTIONS.get(cmd, set()))


class KeyTracker:
    """Track, per dataset, the columns and collapse key needed to resolve a
    ``merge``'s join key at translation time.

    Mirrors the emulator's merge-relevant state so the translator can bake an
    explicit ``on=`` that matches what the emulator would join on:
      - ``cols[name]``         — known columns (seeded with the person key, then
                                 grown by import/generate, reset by collapse).
      - ``collapse_key[name]`` — the key set by a prior ``collapse``/``aggregate``
                                 (the emulator's ``dataset_key_cols``).
      - ``alias_path``         — alias -> registry path, built from the script's
                                 own ``import`` lines, so person-ref FNR linkage
                                 (mother/father/owner -> PERSONID_1) is detected
                                 without an external catalog (same rule as the
                                 emulator's ``_is_person_ref``).

    The person-key seed matches microdata's person-centric default: same-entity
    merges resolve on ``PERSONID_1`` exactly as the emulator does. ``collapse``
    drops that seed (the collapsed frame is keyed by its ``by`` variable).
    """

    DEFAULT_KEY = "PERSONID_1"

    def __init__(self, manifest=None):
        self.cols = {}            # name (None = implicit frame) -> set[str]
        self.collapse_key = {}    # name -> str
        self.alias_path = {}      # alias -> registry path
        self.declared_key = {}    # name -> str (manifest keys[0])
        self.manifest = manifest
        self.source = {}          # name -> (location, format): require URL/path or manifest

    def ensure(self, name):
        if name not in self.cols:
            m = self.manifest
            if m is not None and m.has(name):
                keys = m.keys(name)
                self.cols[name] = set(m.variables(name)) | set(keys)
                if keys:
                    self.declared_key[name] = keys[0]
            else:
                self.cols[name] = {self.DEFAULT_KEY}
        return self.cols[name]

    def create(self, name):
        self.cols.pop(name, None)
        self.collapse_key.pop(name, None)
        self.ensure(name)

    def _key(self, name):
        """Current key for a dataset: collapse key, else manifest-declared."""
        return self.collapse_key.get(name) or self.declared_key.get(name)

    def _keys(self, name):
        """Full declared/collapse key list (composite-aware)."""
        ck = self.collapse_key.get(name)
        if ck:
            return [ck]
        m = self.manifest
        if m is not None and m.has(name) and m.keys(name):
            return m.keys(name)
        dk = self.declared_key.get(name)
        return [dk] if dk else []

    def add_cols(self, name, cols):
        self.ensure(name).update(c for c in cols if c)

    def drop_cols(self, name, cols):
        s = self.ensure(name)
        for c in cols:
            s.discard(c)

    def on_import(self, name, alias, path):
        s = self.ensure(name)
        if alias:
            s.add(alias)
            if path:
                self.alias_path[alias] = path

    def on_collapse(self, name, by_var, targets):
        self.ensure(name)
        if by_var:
            self.collapse_key[name] = by_var
            self.cols[name] = {by_var} | {t for t in targets if t}

    def clone(self, src, dst):
        self.cols[dst] = set(self.cols.get(src, {self.DEFAULT_KEY}))
        if src in self.collapse_key:
            self.collapse_key[dst] = self.collapse_key[src]

    def is_person_ref(self, alias):
        path = self.alias_path.get(alias, "")
        return path in m2py._PERSONID_REF_VARS or path.endswith("_FNR")

    def add_source(self, name, location, keys=()):
        self.source[name] = (location, _format_from(location))
        self.cols[name] = set(keys)
        if keys:
            self.declared_key[name] = keys[0]

    def load_spec(self, name):
        """(location, format) for a dataset's data, or None. require-declared
        file/URL sources take precedence, then the manifest."""
        if name in self.source:
            return self.source[name]
        m = self.manifest
        if m is not None and m.has(name):
            return (m.location(name), m.format(name))
        return None

    def resolve(self, active, into, on_var):
        self.ensure(active)
        self.ensure(into)
        return resolve_merge_key(
            source_cols=self.cols[active],
            target_cols=self.cols[into],
            on_var=on_var,
            src_collapse_key=self._key(active),
            tgt_collapse_key=self._key(into),
            is_person_ref=self.is_person_ref,
        )


def _track(tracker, active, instr):
    """Update ``tracker`` for a verb's effect on the active dataset's columns /
    key, so a later ``merge`` resolves against the right column set."""
    cmd, args = instr["command"], instr["args"]
    opts = instr.get("options") or {}
    if cmd == "import" and isinstance(args, dict):
        tracker.on_import(active, args.get("alias"), args.get("var"))
    elif cmd in ("generate", "replace") and isinstance(args, dict):
        tracker.add_cols(active, [args.get("target")])
    elif cmd == "clone-variables" and isinstance(args, dict):
        prefix, suffix = opts.get("prefix", ""), opts.get("suffix", "")
        new = []
        for pair in (args.get("pairs") or []):
            old, nw = (pair[0], pair[1]) if len(pair) >= 2 else (pair[0], pair[0])
            new.append(f"{prefix}{old}{suffix}" if (prefix or suffix) else nw)
        tracker.add_cols(active, new)
    elif cmd in ("collapse", "aggregate") and isinstance(args, dict):
        targets = [t.get("target") or t.get("src") for t in args.get("targets", [])]
        if cmd == "collapse":
            tracker.on_collapse(active, opts.get("by"), targets)
        else:
            tracker.add_cols(active, targets)
    elif cmd == "rename" and isinstance(args, dict):
        tracker.drop_cols(active, [args.get("old")])
        tracker.add_cols(active, [args.get("new")])
    elif cmd == "drop" and isinstance(args, dict):
        tracker.drop_cols(active, args.get("vars") or [])
    elif cmd == "keep" and isinstance(args, dict) and args.get("vars"):
        cols = tracker.ensure(active)
        ek = key_col_from_cols(cols)
        tracker.cols[active] = (set(args["vars"]) | ({ek} if ek else set()))
    # merge into-form col tracking happens in _emit_merge AFTER key resolution,
    # so the merged vars don't create a spurious common join column.


def _expr_polars_ok(expr):
    """An expression is fine for the polars backend if exprcompile maps it
    natively OR every function it calls is a known microdata function / numpy —
    in which case the polars op's pandas-eval fallback handles it. Only genuinely
    unknown function names make it untranslatable."""
    import ast
    try:
        compile_expr(expr)
        return True
    except UnsupportedExpr:
        pass
    known = set(m2py.get_microdata_functions()) | {
        "int", "min", "max", "abs", "round", "len", "str", "float", "where", "np"}
    try:
        tree = ast.parse(m2py._micro_expr_fixup(expr), mode="eval")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id == "np":
                continue                          # numpy fn -> fallback handles it
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name and name not in known:
                return False
    return True


def _check_polars_expr(instr):
    """Raise UnsupportedExpr if the polars backend can neither compile nor fall
    back on this line's expression/condition (i.e. an unknown function)."""
    cmd, args, cond = instr["command"], instr["args"], instr["condition"]
    if cmd in ("generate", "replace"):
        if not isinstance(args, dict) or "expression" not in args:
            raise UnsupportedExpr(f"unexpected {cmd} args shape")
        if not _expr_polars_ok(args["expression"]):
            raise UnsupportedExpr("unknown function in expression")
    if cond and not _expr_polars_ok(cond):
        raise UnsupportedExpr("unknown function in condition")


def _sanitize(name):
    """A valid Python identifier suffix for a dataset name."""
    s = re.sub(r"\W", "_", str(name))
    return s if s and not s[0].isdigit() else "d_" + s


def _dsvar(backend, name):
    """Variable holding the working frame for a named dataset."""
    return f"{'lf' if backend == 'polars' else 'df'}_{_sanitize(name)}"


def _load_dataset(backend, name, source_path, tracker=None):
    """Materialise dataset ``name``: a require/manifest source (read_source) if
    known, else parquet (file mode), else the in-memory ``_load`` helper."""
    var = _dsvar(backend, name)
    spec = tracker.load_spec(name) if tracker is not None else None
    if spec is not None:
        src = f"ops.read_source({spec[0]!r}, {spec[1]!r})"
    elif source_path is not None:
        src = (f'pl.scan_parquet("{name}.parquet")' if backend == "polars"
               else f'pd.read_parquet("{name}.parquet")')
    else:
        src = f"_load({name!r})"
    return f"{var} = {src}"


def _emit(instr, backend, frame=None, known=(), tracker=None, active=None,
          source_path="df"):
    cmd, args, opts, cond = (
        instr["command"], instr["args"], instr["options"], instr["condition"])
    var = frame or ("lf" if backend == "polars" else "df")

    if cmd in ("generate", "replace"):
        if not isinstance(args, dict) or "expression" not in args:
            return None
        return (f"{var} = ops.{cmd}({var}, target={args['target']!r}, "
                f"expression={args['expression']!r}, cond={cond!r})")
    if cmd == "rename":
        return f"{var} = ops.rename({var}, old={args['old']!r}, new={args['new']!r})"
    if cmd == "clone-variables":
        pairs = args.get("pairs") if isinstance(args, dict) else None
        if not pairs:
            return None
        return (f"{var} = ops.clone_variables({var}, pairs={pairs!r}, "
                f"prefix={opts.get('prefix', '')!r}, suffix={opts.get('suffix', '')!r})")
    if cmd == "destring":
        return f"{var} = ops.destring({var}, vars={args['vars']!r})"
    if cmd == "recode":
        return (f"{var} = ops.recode({var}, vars={args['vars']!r}, "
                f"rules={args['rules']!r}, prefix={args.get('prefix')!r})")
    if cmd in ("keep", "drop"):
        vars_ = args.get("vars") or None
        return f"{var} = ops.{cmd}({var}, vars={vars_!r}, cond={cond!r})"
    if cmd in ("collapse", "aggregate"):
        return (f"{var} = ops.{cmd}({var}, targets={args['targets']!r}, "
                f"by={opts.get('by')!r})")
    if cmd == "reshape-to-panel":
        prefixes = args.get("prefixes") if isinstance(args, dict) else None
        if not prefixes:
            return None
        return f"{var} = ops.reshape_to_panel({var}, prefixes={prefixes!r})"
    if cmd == "reshape-from-panel":
        return f"{var} = ops.reshape_from_panel({var})"
    if cmd == "regress-panel-predict":
        if not isinstance(args, (list, tuple)) or len(args) < 2:
            return None
        dep, indep = args[0], list(args[1:])
        effect = next((e for e in ("re", "be", "pooled") if opts.get(e)), "fe")
        if opts.get("random"):
            effect = "re"
        pred = opts.get("predicted")
        pred = "predicted" if pred in (None, True) else pred
        res = opts.get("residuals")
        res = "residuals" if res is True else res
        eff = opts.get("effects")
        eff = "effects" if eff is True else eff
        return (f"{var} = ops.regress_panel_predict({var}, dep={dep!r}, indep={indep!r}, "
                f"effect={effect!r}, predicted={pred!r}, residuals={res!r}, effects={eff!r})")
    if cmd == "ivregress-predict":
        if not isinstance(args, dict) or not args.get("dep") or not args.get("endog"):
            return None
        res = opts.get("residuals")
        res = "residuals" if res is True else res
        pred = opts.get("predicted")
        pred = "predicted" if pred in (None, True) else pred
        return (f"{var} = ops.ivregress_predict({var}, dep={args['dep']!r}, "
                f"exog={args.get('exog', [])!r}, endog={args['endog']!r}, "
                f"instruments={args.get('instruments', [])!r}, "
                f"predicted={pred!r}, residuals={res!r})")
    if cmd in PREDICT:
        if not isinstance(args, (list, tuple)) or len(args) < 2:
            return None
        dep, indep = args[0], list(args[1:])
        res = opts.get("residuals")
        res = "residuals" if res is True else res          # name, or None
        if cmd in PREDICT_BINARY:
            pred = opts.get("predicted")                   # Xβ only if requested
            pred = "predicted" if pred is True else pred
            prob = opts.get("probabilities")
            prob = "probabilities" if prob is True else prob
            return (f"{var} = ops.{PREDICT[cmd]}({var}, dep={dep!r}, indep={indep!r}, "
                    f"predicted={pred!r}, probabilities={prob!r}, residuals={res!r}, "
                    f"noconstant={bool(opts.get('noconstant'))!r})")
        pred = opts.get("predicted")                       # default 'predicted'
        pred = "predicted" if pred in (None, True) else pred
        return (f"{var} = ops.{PREDICT[cmd]}({var}, dep={dep!r}, indep={indep!r}, "
                f"predicted={pred!r}, residuals={res!r}, "
                f"noconstant={bool(opts.get('noconstant'))!r})")
    if cmd == "merge":
        return _emit_merge(args, opts, backend, var, known, tracker, active,
                           source_path)
    return None


def _load_other(name, backend, known, source_path, tracker=None):
    if name in known:
        return [], _dsvar(backend, name)
    other = _dsvar(backend, name)
    spec = tracker.load_spec(name) if tracker is not None else None
    if spec is not None:
        rhs = f"ops.read_source({spec[0]!r}, {spec[1]!r})"
    elif source_path is not None:
        rhs = (f'pl.scan_parquet("{name}.parquet")' if backend == "polars"
               else f'pd.read_parquet("{name}.parquet")')
    else:
        rhs = f"_load({name!r})"
    return [f"{other} = {rhs}"], other


def _emit_merge(args, opts, backend, var, known, tracker, active, source_path="df"):
    """Emit a merge, baking the resolved join key.

    Two forms, mirroring the emulator:
      * into-form ``merge vars into TARGET [on K]`` — bring ``vars`` from the
        active (source) frame into TARGET; TARGET is updated (left=target,
        right=source dedup on key, always a left-join). The active frame is
        unchanged.
      * old-syntax ``merge X [on K]`` — the active frame gains X's columns
        (symmetric join on the entity/common key).
    Unresolved keys are baked as a best guess with a ``# TODO`` flag.
    """
    tracker = tracker or KeyTracker()
    into_form = isinstance(args, dict) and "into" in args

    if into_form:
        into = args["into"]
        vars_ = args.get("vars") or []
        on_var = args.get("on")
        res = tracker.resolve(active, into, on_var)
        tracker.add_cols(into, vars_)               # target gains them post-merge
        load, src = ([], var)                       # right side = active (source)
        tload, tgt = _load_other(into, backend, known, source_path, tracker)
        known.add(into)                             # the merged target now exists
        todo = ("# TODO: verify join key (could not resolve from catalog)\n"
                if res.status != "ok" else "")
        keys = tracker._keys(into)
        if len(keys) > 1 and res.status == "ok" and res.left_on == res.right_on == keys[0]:
            left_on = right_on = keys  # promote to the full composite key list when the manifest declares >1 key
        else:
            left_on, right_on = res.left_on, res.right_on
        call = (f"{tgt} = ops.merge_into({tgt}, {src}, vars={vars_!r}, "
                f"left_on={left_on!r}, right_on={right_on!r})")
        return "\n".join(tload + load + [todo + call]) or None

    # old-syntax: args is a list (name [on key]); active gains other's cols.
    if not args:
        return None
    name = args[0]
    how = "outer" if opts.get("outer_join") else "left"
    on_var = opts.get("on")
    if isinstance(args, list) and "on" in args:
        i = args.index("on")
        on_var = args[i + 1] if i + 1 < len(args) else on_var
    key, todo_msg = _old_syntax_key(tracker, active, name, on_var)
    if not key:
        return None
    load, other = _load_other(name, backend, known, source_path, tracker)
    todo = (todo_msg + "\n") if todo_msg else ""
    call = f"{var} = ops.merge({var}, {other}, on={key!r}, how={how!r})"
    return "\n".join(load + [todo + call])


def _old_syntax_key(tracker, active, other, on_var):
    """Resolve the (symmetric) key for old-syntax ``merge X``: explicit on, else
    the entity key present in both, else a shared column.

    Returns ``(key, todo_comment)``: ``todo_comment`` is ``None`` when the key is
    resolved with confidence, else a ``# TODO`` line (with no trailing newline)
    to bake above the merge call flagging the guess for manual review."""
    if on_var:
        return on_var, None
    ak = tracker._keys(active)
    if ak and all(c in tracker.ensure(other) for c in ak):
        # promote to the full composite key list when the manifest declares >1 key
        return (ak if len(ak) > 1 else ak[0]), None
    acols = tracker.ensure(active)
    ocols = tracker.ensure(other)
    ek = key_col_from_cols(acols)
    if ek and ek in ocols:
        return ek, None
    # Deterministic order (not set-iteration order, which is arbitrary/
    # hash-dependent) so the same script always bakes the same key.
    common = sorted(acols & ocols)
    if not common:
        # nothing tracked in common: fall back to the person key, flag for review
        return KeyTracker.DEFAULT_KEY, "# TODO: verify join key (could not resolve from catalog)"
    if len(common) > 1:
        # More than one shared column: picking one is a guess even though it
        # "worked" in the sense of returning a valid column name — flag it
        # loudly instead of silently baking an arbitrary choice as "ok".
        return common[0], (
            f"# TODO: multiple shared columns ({', '.join(common)}) — verify join key"
        )
    return common[0], None


def _frame_expr(base, cond):
    """The frame an analysis/plot reads: the working frame ``base``, or a
    row-filtered view of it when the verb carries an ``if`` condition (applied via
    the tested ``keep`` op, without mutating the working frame)."""
    if cond:
        return f"ops.keep({base}, vars=None, cond={cond!r})"
    return base


def _emit_analysis(instr, backend, idx, frame=None, print_results=True):
    """Emit an analysis step: compute a result from the (unchanged) working frame
    and store/print it. Returns the code line, or None if unhandled."""
    cmd, args, opts = instr["command"], instr["args"], instr["options"]
    base = frame or ("lf" if backend == "polars" else "df")
    var = _frame_expr(base, instr["condition"])
    res = f"result_{idx}"
    vars_ = list(args) if args else None

    if cmd == "summarize":
        call = (f"ops.summarize({var}, vars={vars_!r}, by={opts.get('by')!r}, "
                f"gini={bool(opts.get('gini'))!r}, iqr={bool(opts.get('iqr'))!r})")
    elif cmd == "tabulate":
        cell = bool(opts.get("cellpct") or opts.get("cell"))
        row = bool(opts.get("rowpct") or opts.get("row"))
        col = bool(opts.get("colpct") or opts.get("col"))
        call = (f"ops.tabulate({var}, vars={vars_!r}, by={opts.get('by')!r}, "
                f"missing={bool(opts.get('missing'))!r}, "
                f"cellpct={cell!r}, rowpct={row!r}, colpct={col!r}, "
                f"chi2={bool(opts.get('chi2'))!r}, "
                f"top={opts.get('top')!r}, bottom={opts.get('bottom')!r})")
    elif cmd == "correlate":
        call = (f"ops.correlate({var}, vars={vars_!r}, "
                f"pairwise={bool(opts.get('pairwise'))!r}, "
                f"covariance={bool(opts.get('covariance'))!r})")
    elif cmd == "summarize-panel":
        call = (f"ops.summarize_panel({var}, vars={vars_!r}, "
                f"gini={bool(opts.get('gini'))!r}, iqr={bool(opts.get('iqr'))!r})")
    elif cmd == "transitions-panel":
        call = f"ops.transitions_panel({var}, vars={vars_!r})"
    elif cmd == "tabulate-panel":
        if not vars_:
            return None
        cell = bool(opts.get("rowpct") or opts.get("row"))
        col = bool(opts.get("colpct") or opts.get("col"))
        call = (f"ops.tabulate_panel({var}, var1={vars_[0]!r}, "
                f"missing={bool(opts.get('missing'))!r}, "
                f"rowpct={cell!r}, colpct={col!r})")
    elif cmd == "normaltest":
        call = f"ops.normaltest({var}, vars={vars_!r})"
    elif cmd == "ci":
        try:
            level = int(opts.get("level", 95))
        except (ValueError, TypeError):
            level = 95
        call = f"ops.ci({var}, vars={vars_!r}, level={level!r})"
    elif cmd == "anova":
        if not vars_ or len(vars_) < 2:
            return None
        call = f"ops.anova({var}, dep={vars_[0]!r}, factors={vars_[1:]!r})"
    elif cmd == "hausman":
        if not vars_ or len(vars_) < 2:
            return None
        call = f"ops.hausman({var}, dep={vars_[0]!r}, indep={vars_[1:]!r})"
    elif cmd in REGRESSION:
        if not vars_ or len(vars_) < 2:
            return None
        call = (f"ops.{REGRESSION[cmd]}({var}, dep={vars_[0]!r}, "
                f"indep={vars_[1:]!r}, noconstant={bool(opts.get('noconstant'))!r})")
    elif cmd in SURVIVAL:
        if not vars_ or len(vars_) < 2:
            return None
        event, duration, covars = vars_[0], vars_[1], vars_[2:]
        if cmd == "cox":
            call = f"ops.cox({var}, event={event!r}, duration={duration!r}, covars={covars!r})"
        else:
            call = f"ops.{SURVIVAL[cmd]}({var}, event={event!r}, duration={duration!r})"
    elif cmd == "regress-panel":
        if not vars_ or len(vars_) < 2:
            return None
        effect = next((e for e in ("re", "be", "pooled") if opts.get(e)), "fe")
        if opts.get("random"):
            effect = "re"
        call = (f"ops.regress_panel({var}, dep={vars_[0]!r}, indep={vars_[1:]!r}, "
                f"effect={effect!r})")
    elif cmd == "regress-panel-diff":
        if not vars_ or len(vars_) < 3:
            return None
        call = (f"ops.regress_panel_diff({var}, dep={vars_[0]!r}, group={vars_[1]!r}, "
                f"treated={vars_[2]!r}, covars={vars_[3:]!r})")
    elif cmd == "ivregress":
        if not isinstance(args, dict) or not args.get("dep") or not args.get("endog"):
            return None
        call = (f"ops.ivregress({var}, dep={args['dep']!r}, exog={args.get('exog', [])!r}, "
                f"endog={args['endog']!r}, instruments={args.get('instruments', [])!r})")
    elif cmd == "mlogit":
        if not vars_ or len(vars_) < 2:
            return None
        call = f"ops.mlogit({var}, dep={vars_[0]!r}, indep={vars_[1:]!r})"
    elif cmd == "rdd":
        if not isinstance(args, dict) or not args.get("dep") or not args.get("runvar"):
            return None
        try:
            cutoff = float(opts.get("cutoff", 0))
        except (ValueError, TypeError):
            cutoff = 0.0
        try:
            poly = int(opts.get("polynomial", 1))
        except (ValueError, TypeError):
            poly = 1
        fuzzy = opts.get("fuzzy")
        fuzzy = None if fuzzy in (None, True) else fuzzy
        call = (f"ops.rdd({var}, dep={args['dep']!r}, runvar={args['runvar']!r}, "
                f"exog={args.get('exog', [])!r}, cutoff={cutoff!r}, "
                f"polynomial={poly!r}, fuzzy={fuzzy!r})")
    else:
        return None
    return f"{res} = {call}" + (f"\nprint({res})" if print_results else "")


def _emit_plot(instr, backend, idx, write, frame=None):
    """Emit a plot step: build a plotly Figure from the (unchanged) working frame
    into ``fig_<idx>``; write it to an HTML file in file mode."""
    cmd, args, opts = instr["command"], instr["args"], instr["options"]
    base = frame or ("lf" if backend == "polars" else "df")
    var = _frame_expr(base, instr["condition"])
    vars_ = args.get("vars") if isinstance(args, dict) else None
    if not vars_:
        return None
    fig = f"fig_{idx}"
    if cmd == "histogram":
        raw = opts.get("bin") or opts.get("nbins")  # microdata option is bin()
        try:
            bins = int(raw) if raw else 30
        except (ValueError, TypeError):
            bins = 30
        call = (f"ops.histogram({var}, vars={vars_!r}, bins={bins}, "
                f"discrete={bool(opts.get('discrete'))!r}, "
                f"percent={bool(opts.get('percent'))!r}, "
                f"density={bool(opts.get('density'))!r}, "
                f"normal={bool(opts.get('normal'))!r})")
    elif cmd == "barchart":
        # statistic comes from the parenthesised (stat) form -> args['stat'];
        # bare `, mean`-style flags are NOT honoured by the emulator, so they
        # remain unhandled options and the line is flagged.
        stat = args.get("stat", "count")
        call = (f"ops.barchart({var}, vars={vars_!r}, stat={stat!r}, "
                f"over={opts.get('over')!r}, "
                f"horizontal={bool(opts.get('horizontal'))!r}, "
                f"stack={bool(opts.get('stack'))!r})")
    elif cmd == "scatter":
        if len(vars_) < 2:
            return None
        by = opts.get("by") or opts.get("color")
        call = f"ops.scatter({var}, vars={vars_!r}, by={by!r})"
    elif cmd == "boxplot":
        call = f"ops.boxplot({var}, vars={vars_!r}, over={opts.get('over')!r})"
    elif cmd == "piechart":
        stat = args.get("stat", "count")     # (percent) via parenthesised stat
        call = f"ops.piechart({var}, vars={vars_!r}, stat={stat!r})"
    elif cmd == "hexbin":
        if len(vars_) < 2:
            return None
        raw = opts.get("bin") or opts.get("nbins")
        try:
            bins = int(raw) if raw else 30
        except (ValueError, TypeError):
            bins = 30
        call = f"ops.hexbin({var}, vars={vars_!r}, bins={bins})"
    elif cmd == "sankey":
        if len(vars_) < 2:
            return None
        call = f"ops.sankey({var}, vars={vars_!r})"
    elif cmd == "coefplot":
        reg_cmd = args.get("reg_cmd", "regress") if isinstance(args, dict) else "regress"
        if reg_cmd not in ("regress", "logit", "probit", "poisson") or len(vars_) < 2:
            return None                      # e.g. `coefplot y x1` -> reg_cmd='y'
        call = (f"ops.coefplot({var}, reg_cmd={reg_cmd!r}, dep={vars_[0]!r}, "
                f"indep={vars_[1:]!r}, standardize={bool(opts.get('standardize'))!r}, "
                f"noconstant={bool(opts.get('noconstant'))!r})")
    else:
        return None
    line = f"{fig} = {call}"
    if write:
        line += f'\n{fig}.write_html("plot_{idx}.html")'
    return line


def _expand_loops(script):
    """Unroll ``for ... end`` loops and apply ``let`` bindings at translate time,
    producing a flat script. microdata loops are statically unrollable (no nested
    for-blocks; semicolon `;` separates nested levels, space zips). Binding
    substitution (`$name`/`${expr}`/`++`) reuses the emulator's own
    ``_substitute_bindings`` for exact fidelity."""
    it = m2py.MicroInterpreter(metadata_path=None)   # used only for substitution
    parser = it.parser
    script = parser.preprocess_script(script)        # join `\` line continuations
    lines = script.splitlines()
    out = []

    def process(seq, depth=0):
        i = 0
        while i < len(seq):
            sub = it._substitute_bindings(seq[i])
            instr = parser.parse_line(sub)
            if not instr:
                out.append(sub)
                i += 1
                continue
            cmd = instr["command"]
            if cmd == "let" and isinstance(instr["args"], dict):
                name, expr = instr["args"].get("name"), instr["args"].get("expression")
                try:
                    val = eval(expr, {"__builtins__": {}}, it._binding_eval_env())
                except Exception:
                    val = expr
                if name:
                    it.bindings[name] = val
                i += 1
                continue
            if cmd == "for" and isinstance(instr["args"], dict) and "levels" in instr["args"]:
                if depth > 0:
                    # The body-scan below locates the matching 'end' by walking
                    # the top-level `lines` array, which is only valid when
                    # `seq is lines` (the outermost call). A `for` found while
                    # already inside another `for`'s body would silently pair
                    # with the wrong 'end' and unroll scrambled/garbage code
                    # (or recurse forever) — fail loudly instead. microdata
                    # loops express multi-dimensional iteration via
                    # semicolon-separated levels within a single `for`, not
                    # via nested for-blocks, so this is always invalid input.
                    raise ValueError(
                        "m2py_translate: nested 'for' loops are not supported "
                        f"({seq[i].strip()!r} appears inside another for-block); "
                        "use semicolon-separated levels in a single 'for' instead"
                    )
                body, j = [], i + 1
                while j < len(lines):
                    bj = parser.parse_line(lines[j].strip())
                    if bj and bj["command"] == "end":
                        break
                    body.append(lines[j])
                    j += 1
                levels = instr["args"]["levels"]

                def step(idx):
                    if idx >= len(levels):
                        process(body, depth=depth + 1)
                        return
                    lvl = levels[idx]
                    vals = lvl["values"]
                    n = len(vals[0]) if vals else 0
                    for k in range(n):
                        for vn, vl in zip(lvl["vars"], vals):
                            it.bindings[vn] = vl[k]
                        step(idx + 1)

                step(0)
                i = j + 1                       # skip the matching 'end'
                continue
            out.append(sub)
            i += 1

    process(lines)
    return "\n".join(out)


def translate(script, backend="pandas", source_path="df", allow_emulated=False,
              manifest=None, print_results=True):
    """Return a runnable Python program (string) for ``script``.

    ``source_path`` names the input parquet stem ("df" -> df.parquet). Pass
    ``None`` to operate on an in-memory ``df`` (pandas) / ``data`` (polars)
    provided by the caller's namespace — used by the test harness. ``datasets``
    (a dict) may also be provided for merge inputs.

    In in-memory mode the emitted program resolves each input dataset through a
    ``_load`` helper: it returns ``datasets[name]`` when present, else (if the
    runtime ``allow_emulated`` flag is true) synthesises it via the emulator, else
    raises ``KeyError``. ``allow_emulated`` here sets that flag's default in the
    emitted file; a caller (e.g. Anvil) can still override it before running.
    """
    parser = m2py.MicroParser()
    script = _expand_loops(script)               # unroll for-loops, apply let bindings

    if backend == "polars":
        header = ["import polars as pl",
                  "from m2py_runtime import polars_ops as ops",
                  "datasets = globals().get('datasets')"]
        implicit = (f'lf = pl.scan_parquet("{source_path}.parquet")' if source_path is not None
                    else "lf = data if isinstance(data, pl.LazyFrame) else pl.LazyFrame(data)")
    else:
        header = ["import pandas as pd",
                  "from m2py_runtime import pandas_ops as ops",
                  "datasets = globals().get('datasets')"]
        implicit = (f'df = pd.read_parquet("{source_path}.parquet")'
                    if source_path is not None else None)

    # In-memory mode: resolve inputs through a _load helper (datasets dict, with
    # an opt-in emulator fallback). File mode reads parquet directly, so no
    # helper is needed.
    if source_path is None:
        copy = "" if backend == "polars" else ".copy()"
        header += [
            f"allow_emulated = globals().get('allow_emulated', {bool(allow_emulated)!r})",
            "def _load(name):",
            "    _df = (datasets or {}).get(name)",
            "    if _df is not None:",
            f"        return _df{copy}",
            "    if allow_emulated:",
            "        print(f\"[m2py] dataset {name!r} not provided - emulating\")",
            "        return ops.emulate_import(name)",
            "    raise KeyError(f\"dataset {name!r} not provided "
            "(pass it in datasets, or set allow_emulated=True)\")",
        ]

    default_frame = "lf" if backend == "polars" else "df"
    body = []
    idx = 0
    active = None          # None = the implicit single working frame (df/lf)
    known = set()          # dataset names that already have an emitted variable
    used_implicit = False  # did any command actually read the implicit frame?
    tracker = KeyTracker(manifest)   # per-dataset cols + key, for baking merge join keys

    def cur():
        nonlocal used_implicit
        if active:
            return _dsvar(backend, active)
        used_implicit = True
        return default_frame

    for line in script.splitlines():
        if not line.strip():
            continue
        instr = parser.parse_line(line)
        if not instr or instr["command"] in ("textblock", "endblock", "end"):
            continue
        cmd, a = instr["command"], instr["args"]

        # ---- labels: display-only in the emulator, so a no-op on the data ----
        if cmd in LABELS:
            body.append(f"# {cmd} (display-only; data keeps codes): {line.strip()}")
            continue

        # ---- import: data is assumed already present on the target; at
        # translation time we only record the columns it brings (for key
        # resolution) and note it as a comment. ----
        if cmd == "import":
            _track(tracker, active, instr)
            body.append(f"# import (data assumed present): {line.strip()}")
            continue

        # ---- require: bind an alias to a manifest source; seed keys/cols ----
        if cmd == "require":
            src = a.get("source") if isinstance(a, dict) else None
            alias = a.get("alias") if isinstance(a, dict) else None
            bound = bool(src and alias and tracker.manifest is not None
                         and tracker.manifest.has(src))
            file_src = bool(src and alias and not bound and _looks_like_source(src))
            if bound:
                tracker.declared_key[alias] = (tracker.manifest.keys(src)[:1] or [None])[0]
                tracker.cols[alias] = set(tracker.manifest.variables(src)) | set(tracker.manifest.keys(src))
            elif file_src:
                _ks = (instr.get("options") or {}).get("keys")
                _keys = _ks.split() if isinstance(_ks, str) else []
                tracker.add_source(alias, src, _keys)
            suffix = (" (bound from manifest)" if bound
                      else " (source)" if file_src else "")
            body.append(f"# {line.strip()}{suffix}")
            continue

        # ---- dataset/session management (switch active / create variables) ----
        if cmd in SESSION:
            if cmd == "create-dataset" and a:
                known.add(a[0]); active = a[0]
                tracker.create(a[0])
                body.append(_load_dataset(backend, a[0], source_path, tracker))
            elif cmd == "use" and a:
                if a[0] not in known:
                    known.add(a[0])
                    body.append(_load_dataset(backend, a[0], source_path, tracker))
                active = a[0]
                tracker.ensure(a[0])
            elif cmd == "clone-dataset" and len(a) >= 2:
                sv, dv = _dsvar(backend, a[0]), _dsvar(backend, a[1])
                body.append(f"{dv} = {sv}" if backend == "polars" else f"{dv} = {sv}.copy()")
                known.add(a[1])
                tracker.clone(a[0], a[1])
            elif cmd == "clone-units" and len(a) >= 2:
                body.append(f"{_dsvar(backend, a[1])} = ops.clone_units({_dsvar(backend, a[0])})")
                known.add(a[1])
                tracker.create(a[1])
            elif cmd == "rename-dataset" and len(a) >= 2:
                body.append(f"{_dsvar(backend, a[1])} = {_dsvar(backend, a[0])}")
                known.discard(a[0]); known.add(a[1])
                tracker.cols[a[1]] = tracker.cols.pop(a[0], {KeyTracker.DEFAULT_KEY})
                if a[0] in tracker.collapse_key:
                    tracker.collapse_key[a[1]] = tracker.collapse_key.pop(a[0])
                if active == a[0]:
                    active = a[1]
            elif cmd == "delete-dataset" and a:
                body.append(f"del {_dsvar(backend, a[0])}")
                known.discard(a[0])
                tracker.cols.pop(a[0], None); tracker.collapse_key.pop(a[0], None)
                if active == a[0]:
                    active = None
            else:
                body.append(f"# UNTRANSLATED ({cmd}): {line.strip()}")
            continue

        bad = _unhandled_options(instr)
        if bad:
            body.append(f"# UNTRANSLATED (unhandled option: {', '.join(bad)}): {line.strip()}")
            continue
        if backend == "polars":
            try:
                _check_polars_expr(instr)
            except UnsupportedExpr as e:
                body.append(f"# UNTRANSLATED (expr: {e}): {line.strip()}")
                continue
        frame = cur()
        if cmd in ANALYSIS:
            idx += 1
            emitted = _emit_analysis(instr, backend, idx, frame, print_results)
        elif cmd in PLOT:
            idx += 1
            emitted = _emit_plot(instr, backend, idx, write=source_path is not None, frame=frame)
        else:
            _track(tracker, active, instr)
            emitted = _emit(instr, backend, frame, known, tracker, active,
                            source_path)
        body.append(emitted if emitted else f"# UNTRANSLATED: {line.strip()}")

    # footer: materialise the final active frame into `df` (+ write in file mode)
    final = cur()
    if backend == "polars":
        footer = [f'df = {final}.collect(engine="streaming")']
        if source_path is not None:
            footer.append('df.write_parquet("result.parquet")')
    else:
        footer = ([] if final == "df" else [f"df = {final}"])
        if source_path is not None:
            footer.append('df.to_parquet("result.parquet")')

    # only set up the implicit df/lf if the script actually reads it (a pure
    # multi-dataset script never does, so we don't require a default source)
    if used_implicit and implicit:
        header.append(implicit)
    return "\n".join(header + [""] + body + [""] + footer) + "\n"


def run(script, datasets, backend="polars", active=None):
    """Translate ``script`` and execute it locally, returning the resulting
    DataFrame (pandas for backend="pandas", polars for "polars").

    ``datasets`` is a dict of name -> pandas.DataFrame; ``active`` names the
    working dataset (defaults to the first). This mirrors what an offline worker
    / Anvil endpoint does: receive the microdata script as a string, translate
    it, and execute the generated code. Convenience for local testing.
    """
    if active is None:
        active = next(iter(datasets))
    code = translate(script, backend=backend, source_path=None)
    if backend == "polars":
        import polars as pl
        ns = {"data": pl.LazyFrame(datasets[active]), "pl": pl,
              "datasets": {k: pl.LazyFrame(v) for k, v in datasets.items()}}
        exec(code, ns)
        return ns["df"]
    import pandas as pd
    ns = {"df": datasets[active].copy(), "pd": pd, "datasets": dict(datasets)}
    exec(code, ns)
    return ns["df"]


def unsupported(script):
    """Return the list of script lines that would be emitted UNTRANSLATED for
    the polars backend (verb unknown or expression uncompilable)."""
    out = []
    parser = m2py.MicroParser()
    for line in _expand_loops(script).splitlines():
        if not line.strip():
            continue
        instr = parser.parse_line(line)
        if not instr or instr["command"] in ("textblock", "endblock", "end"):
            continue
        cmd = instr["command"]
        if cmd not in SUPPORTED:
            out.append(line.strip())
            continue
        if cmd in SESSION or cmd in LABELS:    # always translate (no-op/state)
            continue
        if _unhandled_options(instr):
            out.append(line.strip())
            continue
        try:
            _check_polars_expr(instr)
        except UnsupportedExpr:
            out.append(line.strip())
            continue
        # also flag verbs that parse/options-check but can't actually emit
        # (e.g. coefplot without a reg-command, scatter with one variable)
        if cmd in ANALYSIS:
            emitted = _emit_analysis(instr, "polars", 1)
        elif cmd in PLOT:
            emitted = _emit_plot(instr, "polars", 1, False)
        else:
            emitted = _emit(instr, "polars")
        if not emitted:
            out.append(line.strip())
    return out
