"""
Quick test: COM direct parameter setting for SW design table
Tests doc.Parameter() and doc.ShowConfiguration2() for the actual model.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.config import LOCAL_PATHS

EXCEL = LOCAL_PATHS["excel"]
MODEL = LOCAL_PATHS["sw_model"]

print("=== COM Direct Parameter Test ===")
print(f"Excel: {EXCEL}")
print(f"Model: {MODEL}")

# Read Excel params
import openpyxl
wb = openpyxl.load_workbook(EXCEL, data_only=True)
ws = wb.active
row2_cells = list(ws.iter_rows(min_row=2, max_row=2))
row2 = [cell.value for cell in row2_cells[0]]
excel_params = [str(v).strip() for v in row2[1:] if v is not None and str(v).strip()]
print(f"\nExcel params: {excel_params}")

config_data = {}
for row in ws.iter_rows(min_row=3, values_only=True):
    if row[0] is None: break
    try:
        config_data[int(row[0])] = [float(row[i]) for i in range(1, len(excel_params)+1)]
    except (ValueError, TypeError, IndexError) as e:
        print(f"Skipping invalid row {row}: {type(e).__name__}: {e}")
wb.close()
print(f"Configs from Excel: {sorted(config_data.keys())}")

# Connect SW
import win32com.client, pythoncom
pythoncom.CoInitialize()

try:
    try:
        sw = win32com.client.GetActiveObject("SldWorks.Application")
        print("\nConnected to running SW")
    except Exception:
        sw = win32com.client.Dispatch("SldWorks.Application")
        sw.Visible = True
        print("Started new SW")
        time.sleep(8)

    oe = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    ow = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    # Test 1: Open with SILENT (current behavior)
    print("\n--- Test 1: OpenDoc6 with SILENT=1 ---")
    doc = sw.OpenDoc6(MODEL, 1, 1, "", oe, ow)
    if doc is None:
        print("FAIL: Cannot open model")
        sys.exit(1)
    print(f"Opened: Errors={oe.value} Warnings={ow.value}")
    result = doc.InsertFamilyTableOpen(EXCEL)
    print(f"InsertFamilyTableOpen (SILENT): {result}")
    sw.CloseDoc(os.path.basename(MODEL))

    # Test 2: Open WITHOUT silent (Options=0)
    print("\n--- Test 2: OpenDoc6 with Options=0 (non-silent) ---")
    oe2 = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    ow2 = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc2 = sw.OpenDoc6(MODEL, 1, 0, "", oe2, ow2)
    if doc2 is None:
        print("FAIL: Cannot open model (non-silent)")
    else:
        print(f"Opened: Errors={oe2.value} Warnings={ow2.value}")
        result2 = doc2.InsertFamilyTableOpen(EXCEL)
        print(f"InsertFamilyTableOpen (NON-SILENT): {result2}")

        if not result2:
            print("\n--- Test 3: EditRebuild3 then InsertFamilyTableOpen ---")
            try:
                doc2.EditRebuild3()
                print("EditRebuild3: OK")
                result3 = doc2.InsertFamilyTableOpen(EXCEL)
                print(f"InsertFamilyTableOpen (after rebuild): {result3}")
            except Exception as e:
                print(f"Failed: {e}")

        # Test 4: COM direct parameter setting
        print("\n--- Test 4: COM Direct Parameter Setting ---")

        # Test parameter access
        print("Testing individual parameter access:")
        all_ok = True
        for pname in excel_params:
            try:
                p = doc.Parameter(pname)
                if p is not None:
                    print(f"  [OK] {pname}: Value={p.Value}, SystemValue={p.SystemValue}")
                else:
                    print(f"  [FAIL] {pname}: Parameter is None")
                    all_ok = False
            except Exception as e:
                print(f"  [FAIL] {pname}: {type(e).__name__}: {e}")
                all_ok = False

        if not all_ok:
            print("\nSome parameters not found. Aborting COM direct test.")
        else:
            # Test config activation and parameter setting
            print("\nTesting config activation and parameter setting:")
            test_config = sorted(config_data.keys())[0]
            cfg_str = str(test_config)
            print(f"Activating config: {cfg_str}")

            try:
                doc.ShowConfiguration2(cfg_str)
                print(f"  [OK] Activated config {cfg_str}")
            except Exception as e:
                print(f"  [FAIL] ShowConfiguration2: {type(e).__name__}: {e}")
                # Try with different format
                try:
                    doc.ShowConfiguration2(str(test_config))
                    print(f"  [OK] Activated (retry string) config {test_config}")
                except Exception as e2:
                    print(f"  [FAIL] Retry also failed: {e2}")
                    pythoncom.CoUninitialize()
                    sys.exit(1)

            # Set parameters
            print("Setting parameter values:")
            vals = config_data[test_config]
            for pname, val in zip(excel_params, vals):
                try:
                    p = doc.Parameter(pname)
                    old_val = p.Value
                    p.Value = val
                    new_val = p.Value
                    print(f"  [OK] {pname}: {old_val} -> {val} (now {new_val})")
                except Exception as e:
                    print(f"  [FAIL] {pname}: {type(e).__name__}: {e}")

        print("\n=== Test Complete ===")
        print("If all [OK] above, COM direct approach works.")
        print("If InsertFamilyTableOpen returned True, standard approach works too.")

    # Cleanup
    try:
        sw.CloseDoc(os.path.basename(MODEL))
    except Exception:
        pass

finally:
    pythoncom.CoUninitialize()
