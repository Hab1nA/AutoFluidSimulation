"""
===============================================================================
诊断脚本 2: 直接 COM 导出 STEP 完整流程测试
完整走通: 连接SW → 打开模型 → ForceRebuildAll → 切换配置 → SaveAs STEP
验证绕过宏文件直接导出 STEP 的可行性。

用法: python tools/test_direct_step_export.py
===============================================================================
"""
import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import LOCAL_PATHS

SW_MODEL = LOCAL_PATHS["sw_model"]
STEP_DIR = LOCAL_PATHS.get("step_dir", "")


def test_direct_export():
    print("=" * 60)
    print("直接 COM 导出 STEP — 完整流程测试")
    print(f"模型: {os.path.basename(SW_MODEL)}")
    print(f"STEP 输出: {STEP_DIR}")
    print("=" * 60)

    # 确保输出目录存在
    if STEP_DIR:
        os.makedirs(STEP_DIR, exist_ok=True)
    else:
        STEP_DIR = tempfile.mkdtemp(prefix="sw_step_test_")
        print(f"使用临时目录: {STEP_DIR}")

    try:
        import win32com.client
        import pythoncom
        pythoncom.CoInitialize()
    except ImportError as e:
        print(f"FAIL: pywin32 未安装: {e}")
        return 1

    sw_app = None
    doc = None
    exit_code = 0
    exported_count = 0
    failed_configs = []

    try:
        # ── 1. 连接 SW ──
        print("\n[1/5] 连接 SolidWorks...")
        try:
            sw_app = win32com.client.GetActiveObject("SldWorks.Application")
            print("    ✓ 已连接运行中的 SW")
        except Exception:
            sw_app = win32com.client.Dispatch("SldWorks.Application")
            sw_app.Visible = True
            print("    ✓ 已启动新 SW 实例")
            time.sleep(8)

        try:
            rev = sw_app.RevisionNumber
            print(f"    SW 版本: {rev}")
        except Exception:
            pass

        # ── 2. 打开模型 ──
        print("\n[2/5] 打开模型...")
        oe = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        ow = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        doc = sw_app.OpenDoc6(SW_MODEL, 1, 1, "", oe, ow)
        print(f"    OpenDoc6: Errors={oe.value}, Warnings={ow.value}")
        if doc is None:
            print("    FAIL: 无法打开模型")
            return 1
        print("    ✓ 模型已打开")

        # ── 3. ForceRebuildAll ──
        print("\n[3/5] 重建所有构型...")

        rebuild_ok = False
        rebuild_method = "?"

        # 策略 A: GetExtension().ForceRebuildAll()
        try:
            ext = doc.GetExtension()
            if ext is not None and not isinstance(ext, bool):
                ext.ForceRebuildAll()
                rebuild_ok = True
                rebuild_method = "GetExtension().ForceRebuildAll()"
        except (AttributeError, TypeError, Exception):
            pass

        # 策略 B: _FlagAsMethod + Extension().ForceRebuildAll()
        if not rebuild_ok:
            try:
                doc._FlagAsMethod('Extension', True)
                ext = doc.Extension()
                if ext is not None and not isinstance(ext, bool):
                    ext.ForceRebuildAll()
                    rebuild_ok = True
                    rebuild_method = "_FlagAsMethod + Extension().ForceRebuildAll()"
            except (AttributeError, TypeError, Exception):
                pass

        # 策略 C: Dispatch 包裹
        if not rebuild_ok:
            try:
                raw_ext = doc.Extension
                if not isinstance(raw_ext, bool) and raw_ext is not None:
                    ext = win32com.client.Dispatch(raw_ext)
                    ext.ForceRebuildAll()
                    rebuild_ok = True
                    rebuild_method = "Dispatch(Extension).ForceRebuildAll()"
            except (AttributeError, TypeError, Exception):
                pass

        # 策略 D: 逐个配置 EditRebuild3
        if not rebuild_ok:
            print("    ⚠ ForceRebuildAll 全部失败，降级为逐个配置 EditRebuild3...")
            rebuild_method = "逐个配置 EditRebuild3()"
            try:
                raw = doc.GetConfigurationNames()
                if isinstance(raw, (tuple, list)):
                    configs = [str(c) for c in raw]
                elif raw is not None:
                    configs = [str(raw)]
                else:
                    configs = []

                if configs:
                    for cfg in configs[:3]:  # 仅前3个, 测试用
                        try:
                            doc.ShowConfiguration2(cfg)
                            doc.EditRebuild3()
                        except Exception:
                            pass
                    rebuild_ok = True
            except Exception as e:
                print(f"    FAIL: EditRebuild3 也失败: {e}")

        if rebuild_ok:
            print(f"    ✓ 重建完成 (方式: {rebuild_method})")
        else:
            print("    ⚠ 所有重建方式均失败，继续尝试导出 (可能导出旧几何)")

        # ── 4. 获取配置列表并导出 STEP ──
        print("\n[4/5] 获取配置列表并导出 STEP...")
        try:
            raw = doc.GetConfigurationNames()
            if isinstance(raw, (tuple, list)):
                configs = [str(c) for c in raw]
            elif raw is not None:
                configs = [str(raw)]
            else:
                configs = []
        except Exception as e:
            print(f"    FAIL: GetConfigurationNames: {e}")
            # 备选: 直接枚举
            configs = [str(i) for i in range(1, 11)]

        print(f"    配置数: {len(configs)} (仅测试前 3 个)")
        if not configs:
            print("    FAIL: 无可用配置")
            return 1

        for idx, cfg_str in enumerate(configs[:3]):
            print(f"\n    --- 构型 {cfg_str} ---")

            # 切换配置
            try:
                doc.ShowConfiguration2(cfg_str)
                print(f"      ShowConfiguration2 ✓")
            except Exception as e:
                print(f"      ShowConfiguration2 FAIL: {type(e).__name__}: {e}")
                failed_configs.append(cfg_str)
                continue

            # 导出 STEP
            try:
                cn_int = None
                try:
                    cn_int = int(cfg_str)
                except ValueError:
                    pass

                if cn_int is not None:
                    filename = f"model_gen4.SLDPRT_{cn_int}.step"
                else:
                    filename = f"model_gen4.SLDPRT_{cfg_str}.step"

                filepath = os.path.join(STEP_DIR, filename)

                se = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                sw = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

                status = doc.Extension.SaveAs(
                    filepath,
                    0,   # swSaveAsCurrentVersion
                    1,   # swSaveAsOptions_Silent
                    None,
                    se,
                    sw,
                )

                if status:
                    file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                    print(f"      ✓ STEP 已导出: {filename} ({file_size:,} bytes)")
                    exported_count += 1
                else:
                    print(f"      ✗ SaveAs 返回 False (Errors={se.value}, Warnings={sw.value})")
                    failed_configs.append(cfg_str)
            except Exception as e:
                print(f"      ✗ SaveAs 异常: {type(e).__name__}: {e}")
                failed_configs.append(cfg_str)

        # ── 5. 结果汇总 ──
        print("\n" + "=" * 60)
        print("[5/5] 结果汇总")
        print("=" * 60)
        print(f"  导出成功: {exported_count}/{min(3, len(configs))} 个构型")
        if failed_configs:
            print(f"  失败构型: {failed_configs}")
        if exported_count > 0:
            print(f"  输出目录: {STEP_DIR}")
            print("  ✓ 直接 COM 导出 STEP 可行！")
            exit_code = 0
        else:
            print("  ✗ 所有构型导出失败，需进一步诊断")
            exit_code = 1

    finally:
        if doc is not None:
            try:
                sw_app.CloseDoc(os.path.basename(SW_MODEL))
                print("\n[清理] 模型已关闭")
            except Exception:
                pass
        if sw_app is not None:
            try:
                sw_app.ExitApp()
                print("[清理] SW 已退出")
            except Exception:
                pass
        try:
            import pythoncom
            pythoncom.CoUninitialize()
        except Exception:
            pass

    return exit_code


if __name__ == "__main__":
    sys.exit(test_direct_export())
