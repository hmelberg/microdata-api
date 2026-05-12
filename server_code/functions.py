"""
Microdata.no-funksjoner for bruk i generate-, replace- og if-uttrykk.
Funksjonene støtter både skalarer og pandas Series (element-vis).
"""
from datetime import date as _pydate, timedelta
import math
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from scipy.special import comb as scipy_comb, gammaln

# --- Hjelpefunksjon: elementvis for skalar og Series ---
def _elementwise(fn):
    """Wrapper: fn(x) virker på både skalar og Series."""
    def wrapped(x, *args, **kwargs):
        if isinstance(x, pd.Series):
            return x.apply(lambda v: fn(v, *args, **kwargs) if pd.notna(v) else np.nan)
        return fn(x, *args, **kwargs) if (x is None or (isinstance(x, float) and np.isnan(x)) is False) else np.nan
    return wrapped

def _safe(fn):
    """Wrapper: returner NaN ved exception."""
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, TypeError, ZeroDivisionError):
            return np.nan
    return wrapped

# ============ MATEMATIKK ============
def acos(x): return np.arccos(x)
def asin(x): return np.arcsin(x)
def atan(x): return np.arctan(x)
def cos(x): return np.cos(x)
def sin(x): return np.sin(x)
def tan(x): return np.tan(x)
def sqrt(x): return np.sqrt(x)
def exp(x): return np.exp(x)
def ln(x): return np.log(x)
def log10(x): return np.log10(x)
def abs_(x): return np.abs(x)  # abs er Python builtin, bruk abs_ eller alias
abs = np.abs  # Pandas eval kan bruke np.abs
def ceil(x): return np.ceil(x)
def floor(x): return np.floor(x)
def int_(x):
    """Elementvis heltallstrunkering. Bevarer int-dtype for pandas Series."""
    if isinstance(x, pd.Series):
        # Nullable Int64 bevarer NaN → pd.NA
        return np.trunc(x).astype('Int64')
    if hasattr(x, '__iter__') and not isinstance(x, str):
        return np.trunc(x).astype(np.int64)
    return int(np.trunc(x))
def round_(x, y=1): return np.round(x / y) * y if y != 1 else np.round(x)
def pi(): return np.pi

@_safe
def comb(x, y):
    """Kombinasjoner C(x,y) = x!/(y!(x-y)!)."""
    if hasattr(x, '__iter__') and not isinstance(x, str):
        return pd.Series(x).apply(lambda v: scipy_comb(int(v), int(y)) if pd.notna(v) else np.nan)
    return scipy_comb(int(x), int(y))

@_safe
def lnfactorial(x):
    """ln(x!)."""
    if hasattr(x, '__iter__') and not isinstance(x, str):
        return pd.Series(x).apply(lambda v: gammaln(v + 1) if pd.notna(v) else np.nan)
    return gammaln(x + 1)

@_safe
def logit(x):
    """ln(x/(1-x)), x in (0,1)."""
    return np.log(x / (1 - x))

# ============ DATOBEHANDLING ============
_EPOCH = _pydate(1970, 1, 1)

def date(y, m, d):
    """Antall dager fra 1970-01-01."""
    if isinstance(y, pd.Series):
        m_s = m if isinstance(m, pd.Series) else pd.Series(m, index=y.index)
        d_s = d if isinstance(d, pd.Series) else pd.Series(d, index=y.index)
        def _d(ry, rm, rd):
            try:
                return (_pydate(int(ry), int(rm), int(rd)) - _EPOCH).days
            except (ValueError, TypeError):
                return np.nan
        return pd.concat([y, m_s, d_s], axis=1).apply(lambda r: _d(r.iloc[0], r.iloc[1], r.iloc[2]), axis=1)
    return (_pydate(int(y), int(m), int(d)) - _EPOCH).days

def _days_to_dt(x):
    """Konverter dager siden epoch til pandas datetime."""
    if isinstance(x, pd.Series):
        return pd.to_datetime(x, unit='D', origin='unix')
    return pd.Timestamp('1970-01-01') + pd.Timedelta(days=int(x))

def year(x):
    if isinstance(x, pd.Series):
        return pd.to_datetime(x, unit='D', origin='unix').dt.year
    return (_EPOCH + timedelta(days=int(x))).year

def month(x):
    if isinstance(x, pd.Series):
        return pd.to_datetime(x, unit='D', origin='unix').dt.month
    return (_EPOCH + timedelta(days=int(x))).month

def day(x):
    if isinstance(x, pd.Series):
        return pd.to_datetime(x, unit='D', origin='unix').dt.day
    return (_EPOCH + timedelta(days=int(x))).day

def week(x):
    if isinstance(x, pd.Series):
        ical = pd.to_datetime(x, unit='D', origin='unix').dt.isocalendar()
        return ical['week'] if 'week' in ical.columns else ical.iloc[:, 1]
    return (_EPOCH + timedelta(days=int(x))).isocalendar()[1]

def halfyear(x):
    m = month(x)
    return ((m - 1) // 6) + 1

def quarter(x):
    if isinstance(x, pd.Series):
        return (pd.to_datetime(x, unit='D', origin='unix').dt.month - 1) // 3 + 1
    return ((_EPOCH + timedelta(days=int(x))).month - 1) // 3 + 1

def dow(x):
    """Dag i uken 1-7 (1=mandag)."""
    if isinstance(x, pd.Series):
        return pd.to_datetime(x, unit='D', origin='unix').dt.dayofweek + 1
    return (_EPOCH + timedelta(days=int(x))).weekday() + 1

def doy(x):
    """Dag i året 1-366."""
    if isinstance(x, pd.Series):
        return pd.to_datetime(x, unit='D', origin='unix').dt.dayofyear
    return (_EPOCH + timedelta(days=int(x))).timetuple().tm_yday

def isoformatdate(x):
    """Konverter dager siden 1970 til YYYY-MM-DD."""
    if isinstance(x, pd.Series):
        return pd.to_datetime(x, unit='D', origin='unix').dt.strftime('%Y-%m-%d')
    d = _EPOCH + timedelta(days=int(x))
    return d.strftime('%Y-%m-%d')

# ============ SANNOLIKHET (scipy.stats) ============
def normal(x): return scipy_stats.norm.cdf(x)
def normalden(x, mu=0, sigma=1): return scipy_stats.norm.pdf(x, mu, sigma)

def chi2(x, v): return scipy_stats.chi2.cdf(x, v)
def chi2den(x, v): return scipy_stats.chi2.pdf(x, v)
def chi2tail(x, v): return scipy_stats.chi2.sf(x, v)
def invchi2(x, v): return scipy_stats.chi2.ppf(x, v)
def invchi2tail(x, v): return scipy_stats.chi2.isf(x, v)

def t(x, v): return scipy_stats.t.cdf(x, v)
def tden(x, v): return scipy_stats.t.pdf(x, v)
def ttail(x, v): return scipy_stats.t.sf(x, v)
def invt(x, v): return scipy_stats.t.ppf(x, v)
def invttail(x, v): return scipy_stats.t.isf(x, v)

def F(x, v1, v2, lam=0): 
    if lam != 0:
        return scipy_stats.ncf.cdf(x, v1, v2, lam)
    return scipy_stats.f.cdf(x, v1, v2)
def Fden(x, v1, v2): return scipy_stats.f.pdf(x, v1, v2)
def Ftail(x, v1, v2, lam=0):
    if lam != 0:
        return scipy_stats.ncf.sf(x, v1, v2, lam)
    return scipy_stats.f.sf(x, v1, v2)
def invF(x, v1, v2): return scipy_stats.f.ppf(x, v1, v2)
def invFtail(x, v1, v2): return scipy_stats.f.isf(x, v1, v2)

def binomial(x, n, p): return scipy_stats.binom.cdf(int(x), int(n), p)
def binomialp(x, n, p): return scipy_stats.binom.pmf(int(x), int(n), p)
def binomialtail(x, n, p): return scipy_stats.binom.sf(int(x), int(n), p)

def betaden(x, a, b): return scipy_stats.beta.pdf(x, a, b)
def ibeta(x, a, b): return scipy_stats.beta.cdf(x, a, b)
def ibetatail(x, a, b): return scipy_stats.beta.sf(x, a, b)
def invibeta(x, a, b): return scipy_stats.beta.ppf(x, a, b)
def invibetatail(x, a, b): return scipy_stats.beta.isf(x, a, b)

# ============ STRENG ============
def length(x):
    if isinstance(x, pd.Series):
        return x.astype(str).str.len()
    return len(str(x))

def string(x):
    if isinstance(x, pd.Series):
        return x.astype(str)
    return str(x)

def lower(x):
    if isinstance(x, pd.Series):
        return x.astype(str).str.lower()
    return str(x).lower()

def upper(x):
    if isinstance(x, pd.Series):
        return x.astype(str).str.upper()
    return str(x).upper()

def _substr_prepare_scalar(v):
    """Heltalls-IDer som kommunenummer (0–9999) skal ha 4 tegn før substr, ellers mister man ledende null (301→«0301»)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ''
    if isinstance(v, (bool, np.bool_)):
        return str(v)
    if isinstance(v, (int, np.integer)):
        iv = int(v)
        if 0 <= iv <= 9999:
            return f"{iv:04d}"
        return str(iv)
    if isinstance(v, (float, np.floating)) and v == int(v):
        iv = int(v)
        if 0 <= iv <= 9999:
            return f"{iv:04d}"
        return str(iv)
    return str(v)


def substr(x, pos, length):
    """pos: 1-basert. Negativ pos = fra slutten."""
    if isinstance(x, pd.Series):
        def _sub_one(v, p, n):
            s = _substr_prepare_scalar(v)
            if p < 0:
                p = len(s) + p + 1
            return s[p - 1 : p - 1 + n] if p >= 1 else ''

        p, n = int(pos), int(length)
        return x.apply(lambda v: _sub_one(v, p, n))
    s = _substr_prepare_scalar(x)
    pos, length = int(pos), int(length)
    if pos < 0:
        pos = len(s) + pos + 1
    return s[pos - 1 : pos - 1 + length] if pos >= 1 else ''

def trim(x):
    if isinstance(x, pd.Series):
        return x.astype(str).str.strip()
    return str(x).strip()

def ltrim(x):
    if isinstance(x, pd.Series):
        return x.astype(str).str.lstrip()
    return str(x).lstrip()

def rtrim(x):
    if isinstance(x, pd.Series):
        return x.astype(str).str.rstrip()
    return str(x).rstrip()

def startswith(x, s):
    if isinstance(x, pd.Series):
        return x.astype(str).str.startswith(str(s))
    return str(x).startswith(str(s))

def endswith(x, s):
    if isinstance(x, pd.Series):
        return x.astype(str).str.endswith(str(s))
    return str(x).endswith(str(s))

# ============ LOGIKK ============
def inlist(x, *vals):
    if isinstance(x, pd.Series):
        return x.isin(list(vals))
    # x is scalar, vals may be Series (e.g. inlist("0", col1, col2, ...))
    if any(isinstance(v, pd.Series) for v in vals):
        base = next(v for v in vals if isinstance(v, pd.Series))
        result = pd.Series(False, index=base.index)
        for v in vals:
            result = result | (v == x)
        return result
    return x in vals

def inrange(x, lo, hi):
    return (x >= lo) & (x <= hi)

def sysmiss(x):
    return pd.isna(x)

# ============ ROW-* (flere variabler) ============
def rowmax(*cols):
    df = pd.concat(cols, axis=1)
    return df.max(axis=1)

def rowmin(*cols):
    df = pd.concat(cols, axis=1)
    return df.min(axis=1)

def rowmean(*cols):
    df = pd.concat(cols, axis=1)
    return df.mean(axis=1)

def rowmedian(*cols):
    df = pd.concat(cols, axis=1)
    return df.median(axis=1)

def rowtotal(*cols):
    df = pd.concat(cols, axis=1)
    return df.sum(axis=1)

def rowstd(*cols):
    df = pd.concat(cols, axis=1)
    return df.std(axis=1)

def rowmissing(*cols):
    df = pd.concat(cols, axis=1)
    return df.isna().sum(axis=1)

def rowvalid(*cols):
    df = pd.concat(cols, axis=1)
    return df.notna().sum(axis=1)

def rowconcat(*cols):
    """Konkatenerer strenger radvis.

    Støtter både Series og skalarer (f.eks. ' ' i rowconcat(fornavn, ' ', etternavn)):
    skalarer broadcastes til Series med samme index som første Series-argument.
    """
    if not cols:
        return pd.Series([], dtype=str)
    # Finn første Series for å få index
    first_series = next((c for c in cols if isinstance(c, pd.Series)), None)
    if first_series is None:
        # Bare skalarer: returner én rad med sammenslått streng
        return pd.Series([''.join(str(c) for c in cols)])
    series_cols = []
    for c in cols:
        if isinstance(c, pd.Series):
            s = c
        else:
            # Broadcast skalar til Series
            s = pd.Series([c] * len(first_series), index=first_series.index)
        series_cols.append(s.astype(str).fillna(''))
    df = pd.concat(series_cols, axis=1)
    return df.agg(''.join, axis=1)

# ============ LABEL-FUNKSJONER ============
# Referanser settes av MicroInterpreter etter opprettelse (trådsikker for enkeltbruk)
_label_manager_ref = [None]
_bindings_ref = [None]

def set_label_manager(lm):
    """Setter global LabelManager-referanse for bruk i eval-uttrykk."""
    _label_manager_ref[0] = lm

def set_bindings(b):
    """Setter global bindings-referanse for bind()-funksjonen i eval-uttrykk."""
    _bindings_ref[0] = b

def label_to_code(varname, label_text):
    """Konverter en labeltekst til tilhørende kode for variabel varname.
    varname er kolonnenavn som streng (eller Series med .name satt).
    Returnerer koden (tall/streng) eller NaN hvis ikke funnet."""
    lm = _label_manager_ref[0]
    if lm is None:
        return np.nan
    if isinstance(varname, pd.Series):
        varname = varname.name
    cl = lm.get_codelist_for_var(str(varname) if varname is not None else '')
    if not cl:
        return np.nan
    lt = str(label_text)
    for code, lbl in cl.items():
        if str(lbl) == lt:
            return code
    return np.nan

def inlabels(series, *labels):
    """Sjekker om verdiens label (fra kodeliste) er i listen av labeltekster.
    Identifiserer variabelen via Series.name."""
    lm = _label_manager_ref[0]
    if not isinstance(series, pd.Series):
        return series in labels
    varname = getattr(series, 'name', None)
    cl = lm.get_codelist_for_var(varname) if (lm and varname) else None
    label_set = {str(l) for l in labels}
    def _check(v):
        if pd.isna(v):
            return False
        display = str(cl.get(v, v)) if cl else str(v)
        return display in label_set or str(v) in label_set
    return series.apply(_check)

def labelcontains(series, substring):
    """Sjekker om verdiens label inneholder substring."""
    lm = _label_manager_ref[0]
    if not isinstance(series, pd.Series):
        return str(substring) in str(series)
    varname = getattr(series, 'name', None)
    cl = lm.get_codelist_for_var(varname) if (lm and varname) else None
    sub = str(substring)
    def _check(v):
        if pd.isna(v):
            return False
        display = str(cl.get(v, v)) if cl else str(v)
        return sub in display
    return series.apply(_check)

def to_symbol(s):
    """Konverter til symbol-type (streng i Python)."""
    if isinstance(s, pd.Series):
        return s.astype(str)
    return str(s)

def bind(name):
    """Returnerer verdien til en let-binding ved navn.

    Microdata-semantikk: `bind(kpi)` slår opp bindingen kpi. I m2py kommer
    argumentet inn allerede evaluert (siden eval kjøres med bindinger i miljøet,
    blir bare `y` til verdien sin før bind kalles). For å støtte begge formene:
      - streng som matcher en binding -> oppslag
      - allerede en verdi (tall/streng/None) -> returner uendret
    """
    bindings = _bindings_ref[0]
    if isinstance(name, str) and bindings is not None and name in bindings:
        return bindings[name]
    return name

# ============ ANDRE ============
def quantile(x, n):
    """Grupperer i n grupper (2-100), returnerer gruppe 0 til n-1."""
    if isinstance(x, pd.Series):
        return pd.qcut(x, int(n), labels=False, duplicates='drop')
    return np.nan

def to_int(x):
    if isinstance(x, pd.Series):
        return pd.to_numeric(x, errors='coerce')
    try:
        return int(float(x))
    except (ValueError, TypeError):
        return np.nan

def to_str(x):
    if isinstance(x, pd.Series):
        return x.astype(str)
    return str(x)

def date_fmt(y, m=1, d=1):
    """yyyy-mm-dd fra år, måned, dag. Brukes i let."""
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def get_microdata_functions(label_manager=None):
    """
    Returnerer dict med alle microdata-funksjoner for injeksjon i eval.
    Bruk: df.eval(expr, engine='python', local_dict={**get_microdata_functions(), 'np': np})
    """
    return {
        # Matematikk
        'acos': acos, 'asin': asin, 'atan': atan,
        'cos': cos, 'sin': sin, 'tan': tan,
        'sqrt': sqrt, 'exp': exp, 'ln': ln, 'log10': log10,
        'abs': abs, 'ceil': ceil, 'floor': floor, 'int': int_,
        'round': round_, 'pi': pi,
        'comb': comb, 'lnfactorial': lnfactorial, 'logit': logit,
        # Dato
        'date': date, 'year': year, 'month': month, 'day': day,
        'week': week, 'halfyear': halfyear, 'quarter': quarter,
        'dow': dow, 'doy': doy, 'isoformatdate': isoformatdate,
        # Sannsynlighet
        'normal': normal, 'normalden': normalden,
        'chi2': chi2, 'chi2den': chi2den, 'chi2tail': chi2tail,
        'invchi2': invchi2, 'invchi2tail': invchi2tail,
        't': t, 'tden': tden, 'ttail': ttail, 'invt': invt, 'invttail': invttail,
        'F': F, 'Fden': Fden, 'Ftail': Ftail, 'invF': invF, 'invFtail': invFtail,
        'binomial': binomial, 'binomialp': binomialp, 'binomialtail': binomialtail,
        'betaden': betaden, 'ibeta': ibeta, 'ibetatail': ibetatail,
        'invibeta': invibeta, 'invibetatail': invibetatail,
        # Streng
        'length': length, 'string': string,
        'lower': lower, 'upper': upper,
        'substr': substr, 'trim': trim, 'ltrim': ltrim, 'rtrim': rtrim,
        'startswith': startswith, 'endswith': endswith,
        # Logikk
        'inlist': inlist, 'inrange': inrange, 'sysmiss': sysmiss,
        # Row
        'rowmax': rowmax, 'rowmin': rowmin, 'rowmean': rowmean,
        'rowmedian': rowmedian, 'rowtotal': rowtotal, 'rowstd': rowstd,
        'rowmissing': rowmissing, 'rowvalid': rowvalid, 'rowconcat': rowconcat,
        # Andre
        'quantile': quantile, 'to_int': to_int, 'to_str': to_str,
        'date_fmt': date_fmt,
        # Label-funksjoner
        'label_to_code': label_to_code, 'inlabels': inlabels,
        'labelcontains': labelcontains, 'to_symbol': to_symbol, 'bind': bind,
    }
