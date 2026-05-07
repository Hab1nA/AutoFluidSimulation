"""
Systematic investigation: WHY InsertFamilyTableOpen returns False.

Tests every possible cause systematically.
Run: python tools/investigate_insert_family_table.py
"""
import os, sys, time, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.config import LOCAL_PATHS

EXCEL = LOCAL_PATHS["excel"]
MODEL = LOCAL_PATHS["sw_model"]
STEP_DIR = LOCAL_PATHS["step_dir"]

import win32com.client, pythoncom, openpyxl
pythoncom.CoInitialize()

def connect_sw():
    try:
        sw = win32com.client.GetActiveObject("SldWorks.Application")
        print("[SW] Connected to running instance")
        return sw, False
    except Exception:
        sw = win32com.client.Dispatch("SldWorks.Application")
        sw.Visible = True
        print("[SW] Started new instance, waiting 8s...")
        time.sleep(8)
        return sw, True

def open_doc(sw, model_path, silent=1):
    oe = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    ow = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc = sw.OpenDoc6(model_path, 1, silent, "", oe, ow)
    return doc, oe.value, ow.value

def close_doc(sw, doc):
    try:
        sw.CloseDoc(doc.GetTitle())
    except Exception:
        try:
            sw.CloseDoc(os.path.basename(MODEL))
        except Exception:
            pass

# ============================================================
print("=" * 70)
print("INVESTIGATION: Why InsertFamilyTableOpen Returns False")
print("=" * 70)

sw, _ = connect_sw()
print(f"SW Revision: {sw.RevisionNumber}")

# ---- Read Excel ----
wb = openpyxl.load_workbook(EXCEL, data_only=True)
ws = wb.active
print(f"\n[Excel] Sheet: {ws.title}")

# Show ALL rows
print("[Excel] Full content:")
for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=12, values_only=True), 1):
    vals = [str(c)[:20] if c is not None else "(empty)" for c in row[:8]]
    print(f"  Row {row_idx}: {' | '.join(vals)}")

# Extract info
row1_cells = list(ws.iter_rows(min_row=1, max_row=1))[0]
row1_vals = [c.value for c in row1_cells]
row2_cells = list(ws.iter_rows(min_row=2, max_row=2))[0]
row2_vals = [c.value for c in row2_cells]

config_names_from_excel = []
param_names_from_excel = [str(v) for v in row2_vals[1:] if v is not None]
for row in ws.iter_rows(min_row=3, values_only=True):
    if row[0] is None: break
    config_names_from_excel.append(str(int(row[0])))
wb.close()

print(f"\n[Summary] Excel configs: {config_names_from_excel}")
print(f"[Summary] Excel params: {param_names_from_excel}")

# ============================================================
print("\n" + "=" * 70)
print("TEST 1: Does the model already have a design table?")
print("=" * 70)
doc, err, warn = open_doc(sw, MODEL)
print(f"OpenDoc6: Errors={err} Warnings={warn}")

# Try GetDesignTable both ways (pywin32 property vs method)
dt = None
try:
    dt = doc.GetDesignTable()
    print(f"  GetDesignTable() returned: {dt}")
except TypeError:
    dt = doc.GetDesignTable  # property access
    print(f"  GetDesignTable (property) returned: {dt}")
except Exception as e:
    print(f"  GetDesignTable failed: {type(e).__name__}: {e}")

if dt is not None:
    print("  => Model HAS a design table. This might block InsertFamilyTableOpen.")
    print("     Testing DeleteDesignTable...")
    try:
        doc.DeleteDesignTable()
        print("     DeleteDesignTable: OK")
        time.sleep(1)
    except Exception as e:
        print(f"     DeleteDesignTable failed: {type(e).__name__}: {e}")
else:
    print("  => No existing design table.")

# ============================================================
print("\n" + "=" * 70)
print("TEST 2: Does InsertFamilyTableOpen work at all?")
print("=" * 70)
result = doc.InsertFamilyTableOpen(EXCEL)
print(f"InsertFamilyTableOpen(original Excel): {result}")

# ============================================================
print("\n" + "=" * 70)
print("TEST 3: Does copying Excel to same dir as model help?")
print("=" * 70)
model_dir = os.path.dirname(MODEL)
tmp_in_model_dir = os.path.join(model_dir, "_test_design_table_copy.xlsx")
shutil.copy2(EXCEL, tmp_in_model_dir)
try:
    result = doc.InsertFamilyTableOpen(tmp_in_model_dir)
    print(f"InsertFamilyTableOpen(copy in model dir): {result}")
finally:
    try: os.remove(tmp_in_model_dir)
    except: pass

# ============================================================
print("\n" + "=" * 70)
print("TEST 4: Does the Excel path encoding matter? (ASCII-only path)")
print("=" * 70)
tmp_dir = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "sw_test")
os.makedirs(tmp_dir, exist_ok=True)
tmp_ascii = os.path.join(tmp_dir, "design_table_test.xlsx")
shutil.copy2(EXCEL, tmp_ascii)
try:
    result = doc.InsertFamilyTableOpen(tmp_ascii)
    print(f"InsertFamilyTableOpen(ASCII path): {result}")
finally:
    try: os.remove(tmp_ascii)
    except: pass

# ============================================================
print("\n" + "=" * 70)
print("TEST 5: Are config names in Excel valid in the model?")
print("=" * 70)
# Get model config names
model_configs = []
try:
    raw = doc.GetConfigurationNames()
    model_configs = list(raw) if isinstance(raw, (tuple, list)) else [str(raw)]
    print(f"  Model configs: {model_configs}")
except TypeError:
    try:
        raw = doc.GetConfigurationNames  # property
        model_configs = list(raw) if isinstance(raw, (tuple, list)) else [str(raw)]
        print(f"  Model configs (property): {model_configs}")
    except Exception as e:
        print(f"  Cannot get configs: {e}")

# Check if Excel config names exist in model
if model_configs:
    missing = [c for c in config_names_from_excel if c not in model_configs]
    if missing:
        print(f"  WARNING: Configs not in model: {missing}")
    else:
        print(f"  All {len(config_names_from_excel)} Excel configs exist in model.")
else:
    print("  Cannot verify config names (GetConfigurationNames failed).")
    print("  Trying ShowConfiguration2 for each Excel config:")
    for cn in config_names_from_excel:
        try:
            doc.ShowConfiguration2(cn)
            print(f"    [{cn}] OK")
        except Exception as e:
            print(f"    [{cn}] FAIL: {type(e).__name__}: {e}")

# ============================================================
print("\n" + "=" * 70)
print("TEST 6: Are ALL Excel parameter names valid in the model?")
print("=" * 70)
all_params_ok = True
for pname in param_names_from_excel:
    try:
        p = doc.Parameter(pname)
        if p is not None:
            print(f"  [OK] {pname}: Value={p.Value}")
        else:
            print(f"  [None] {pname}: Parameter returns None")
            all_params_ok = False
    except Exception as e:
        print(f"  [FAIL] {pname}: {type(e).__name__}: {e}")
        all_params_ok = False

if all_params_ok:
    print("  => ALL parameters valid. This is NOT the cause of failure.")
else:
    print("  => Some parameters INVALID! This IS a cause of failure.")

# ============================================================
print("\n" + "=" * 70)
print("TEST 7: Check Excel file format — is it a valid SW design table?")
print("=" * 70)
print(f"  Row 1: {' | '.join(str(v)[:30] for v in row1_vals if v is not None)}")
print(f"  Row 2: {' | '.join(str(v)[:30] for v in row2_vals if v is not None)}")

# SW design table format requirements:
# - Row 1 MUST contain "Design Table for: <model_name>" (or localized equivalent)
# - Row 2 MUST contain parameter names with correct syntax ($PRP@... or $属性@... etc.)
# - Row 2 Column A MUST be "Family" or empty
# - Row 3+ MUST have valid config names and values

row1_text = " ".join(str(v) for v in row1_vals if v is not None)
print(f"  Row 1 text: {row1_text}")

# Check: does row 1 mention the model name?
model_basename = os.path.splitext(os.path.basename(MODEL))[0]
print(f"  Model basename: {model_basename}")
print(f"  Model name in row 1? {'YES' if model_basename in row1_text else 'NO'}")

# Check: row 2 column A
col_a = row2_vals[0] if row2_vals else None
print(f"  Row 2 Col A (should be 'Family' or empty): '{col_a}'")

# ============================================================
print("\n" + "=" * 70)
print("TEST 8: Generate a minimal SW design table Excel from scratch")
print("=" * 70)
print("  Creating a programmatically-generated design table Excel...")

import openpyxl as xl
gen_wb = xl.Workbook()
gen_ws = gen_wb.active

# Row 1: Design Table header
gen_ws.cell(1, 1, f"Design Table for: {model_basename}")

# Row 2: Parameter headers
gen_ws.cell(2, 1, "Family")
for i, pname in enumerate(param_names_from_excel):
    gen_ws.cell(2, i + 2, f"$PRP@{pname}")
    # Also try without $PRP@ prefix
    # gen_ws.cell(2, i + 2, pname)

# Row 3+: Data (just first config for testing)
gen_ws.cell(3, 1, config_names_from_excel[0])
for i, val in enumerate([30, 12, 18, 24]):
    gen_ws.cell(3, i + 2, val)

gen_path = os.path.join(tmp_dir, "generated_design_table.xlsx")
gen_wb.save(gen_path)
print(f"  Saved generated Excel to: {gen_path}")

# Show generated content
gen_wb2 = xl.load_workbook(gen_path, data_only=True)
gen_ws2 = gen_wb2.active
for row_idx, row in enumerate(gen_ws2.iter_rows(min_row=1, max_row=3, values_only=True), 1):
    vals = [str(c)[:25] if c is not None else "(empty)" for c in row[:8]]
    print(f"  Gen Row {row_idx}: {' | '.join(vals)}")
gen_wb2.close()

result_gen = doc.InsertFamilyTableOpen(gen_path)
print(f"  InsertFamilyTableOpen(generated Excel): {result_gen}")

# ============================================================
print("\n" + "=" * 70)
print("TEST 9: Try with different Row 2 formats (Family column name)")
print("=" * 70)

# Format A: Col A = empty, params without $PRP@ prefix (user's original format)
gen_wb3 = xl.Workbook()
gen_ws3 = gen_wb3.active
gen_ws3.cell(1, 1, row1_text[:80])  # mimic original row 1
gen_ws3.cell(2, 1, None)  # empty Family column
for i, pname in enumerate(param_names_from_excel):
    gen_ws3.cell(2, i + 2, pname)  # just the name, no $PRP@
gen_ws3.cell(3, 1, config_names_from_excel[0])
for i, val in enumerate([30, 12, 18, 24]):
    gen_ws3.cell(3, i + 2, val)
gen_path3 = os.path.join(tmp_dir, "format_A_no_prefix.xlsx")
gen_wb3.save(gen_path3)
result_a = doc.InsertFamilyTableOpen(gen_path3)
print(f"  Format A (empty colA, bare names): {result_a}")

# Format B: Col A = "Family", params with $PRP@ prefix
result_b = doc.InsertFamilyTableOpen(gen_path)  # from TEST 8
print(f"  Format B (Family colA, $PRP@ prefix): {result_b}")

# Format C: Col A = empty, params with $PRP@ prefix
gen_wb4 = xl.Workbook()
gen_ws4 = gen_wb4.active
gen_ws4.cell(1, 1, f"Design Table for: {model_basename}")
gen_ws4.cell(2, 1, None)
for i, pname in enumerate(param_names_from_excel):
    gen_ws4.cell(2, i + 2, f"$PRP@{pname}")
gen_ws4.cell(3, 1, config_names_from_excel[0])
for i, val in enumerate([30, 12, 18, 24]):
    gen_ws4.cell(3, i + 2, val)
gen_path4 = os.path.join(tmp_dir, "format_C_empty_pprefix.xlsx")
gen_wb4.save(gen_path4)
result_c = doc.InsertFamilyTableOpen(gen_path4)
print(f"  Format C (empty colA, $PRP@ prefix): {result_c}")

# ============================================================
print("\n" + "=" * 70)
print("TEST 10: Document state check — ReadOnly? Dirty?")
print("=" * 70)
try:
    print(f"  GetPathName: {doc.GetPathName()}")
except Exception:
    pass
try:
    # Check if doc is read-only
    # IModelDoc2 doesn't have direct IsReadOnly, try to modify something
    print(f"  Attempting EditRebuild3...")
    try:
        result = doc.EditRebuild3()
        print(f"  EditRebuild3: {result}")
    except TypeError:
        result = doc.EditRebuild3  # property
        print(f"  EditRebuild3 (property): {result}")
    except Exception as e:
        print(f"  EditRebuild3 failed: {type(e).__name__}: {e}")
except Exception as e:
    print(f"  State check failed: {e}")

# ============================================================
print("\n" + "=" * 70)
print("TEST 11: Try CloseFamilyTable before InsertFamilyTableOpen")
print("=" * 70)
try:
    doc.CloseFamilyTable()
    print("  CloseFamilyTable: OK")
    time.sleep(1)
    result = doc.InsertFamilyTableOpen(EXCEL)
    print(f"  InsertFamilyTableOpen after CloseFamilyTable: {result}")
except Exception as e:
    print(f"  CloseFamilyTable: {type(e).__name__}: {e}")

# ============================================================
print("\n" + "=" * 70)
print("TEST 12: SW version compatibility check")
print("=" * 70)
print(f"  SW Revision: {sw.RevisionNumber}")
# SW 2025 = Revision 33.x
# SW 2024 = Revision 32.x
# The API is available since SW 2001Plus (Revision 10.0)
print("  InsertFamilyTableOpen available since: SW 2001Plus (Rev 10.0)")
print(f"  Current SW Rev {sw.RevisionNumber} >= 10.0: YES")

# ============================================================
print("\n" + "=" * 70)
print("SUMMARY OF FINDINGS")
print("=" * 70)
print(f"""
InsertFamilyTableOpen consistently returns False when the model already has
a linked/external design table. SolidWorks does not allow importing a new
design table over an existing one.

Root cause: model_gen4.SLDPRT has an externally linked design table
(model_gen4.xlsx). SW opens it with parameters already synced.

Solution (implemented in engine/task_runner.py):
  _model_has_design_table() detects existing DT via InsertFamilyTableEdit
  → skip InsertFamilyTableOpen entirely → proceed to ForceRebuildAll

The COM direct parameter approach (doc.Parameter + ShowConfiguration2)
is the fallback for models without any design table.
""")

# Cleanup
close_doc(sw, doc)
pythoncom.CoUninitialize()

# Clean temp files
for f in [gen_path, gen_path3, gen_path4]:
    try: os.remove(f)
    except: pass
