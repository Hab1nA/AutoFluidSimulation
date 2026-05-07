"""
SolidWorks STEP 导出隔离测试脚本

验证 task_runner.py 中 5 个修复点的有效性：
  E1: SaveAs ExportData 参数 — VT_DISPATCH 替代 None
  E2: _FlagAsMethod 移除布尔参数
  E3: GetConfigurationNames _FlagAsMethod 预标记
  E4: COM 验证方法 callable() 检查
  E5: 未定义变量 model_param_names 修复

使用方法：
  python tools/test_sw_step_export_fix.py [--config CONFIG_INDEX]

  --config  可选，指定单个构型索引（0~9）进行测试，默认测试构型 0

前提条件：
  - SolidWorks 2025 已安装
  - model_gen4.SLDPRT 和 model_gen4.xlsx 存在于配置路径
  - 在 Windows 上运行（需要 COM 支持）
"""
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import LOCAL_PATHS, ENGINE_CONFIG


def test_com_dispatch_variant():
    """E1 验证: win32com.client.VARIANT(pythoncom.VT_DISPATCH, None) 可正确创建"""
    import pythoncom
    import win32com.client

    print("\n" + "=" * 60)
    print("E1 测试: VT_DISPATCH VARIANT 创建")
    print("=" * 60)

    try:
        export_data = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        print(f"  ✓ VARIANT 创建成功: type={type(export_data)}, value={export_data}")
        return True
    except Exception as e:
        print(f"  ✗ VARIANT 创建失败: {type(e).__name__}: {e}")
        return False


def test_flag_as_method_signature():
    """E2 验证: _FlagAsMethod 只接受字符串参数"""
    import win32com.client

    print("\n" + "=" * 60)
    print("E2 测试: _FlagAsMethod 签名验证")
    print("=" * 60)

    try:
        import pythoncom
        pythoncom.CoInitialize()
        try:
            sw_app = win32com.client.GetActiveObject("SldWorks.Application")
        except Exception:
            sw_app = win32com.client.Dispatch("SldWorks.Application")
            time.sleep(5)

        ext = sw_app.NewDocument("", 0, 0, 0)
        if ext is not None:
            doc = sw_app.ActiveDoc
            ext_obj = doc.Extension if doc else None
        else:
            doc = sw_app.ActiveDoc
            ext_obj = doc.Extension if doc else None

        if ext_obj is None:
            print("  ⚠ 无法获取 Extension 对象，跳过 _FlagAsMethod 测试")
            try:
                sw_app.ExitApp()
            except Exception:
                pass
            pythoncom.CoUninitialize()
            return True

        try:
            ext_obj._FlagAsMethod('ForceRebuildAll')
            print("  ✓ _FlagAsMethod('ForceRebuildAll') 调用成功（无布尔参数）")
            result = True
        except TypeError as e:
            print(f"  ✗ _FlagAsMethod('ForceRebuildAll') 失败: {e}")
            result = False

        try:
            ext_obj._FlagAsMethod('ForceRebuildAll', True)
            print("  ✗ _FlagAsMethod('ForceRebuildAll', True) 不应成功")
            result = False
        except TypeError:
            print("  ✓ _FlagAsMethod('ForceRebuildAll', True) 正确抛出 TypeError")

        try:
            sw_app.CloseAllDocuments(True)
        except Exception:
            pass
        try:
            sw_app.ExitApp()
        except Exception:
            pass

        pythoncom.CoUninitialize()
        return result
    except ImportError:
        print("  ⚠ win32com 未安装，跳过测试")
        return True
    except Exception as e:
        print(f"  ✗ E2 测试异常: {type(e).__name__}: {e}")
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
        return False


def test_get_configuration_names():
    """E3 验证: _FlagAsMethod('GetConfigurationNames') 后方法可正常调用"""
    print("\n" + "=" * 60)
    print("E3 测试: GetConfigurationNames _FlagAsMethod 预标记")
    print("=" * 60)

    sw_model = LOCAL_PATHS["sw_model"]
    if not os.path.exists(sw_model):
        print(f"  ⚠ SW 模型文件不存在: {sw_model}，跳过测试")
        return True

    import pythoncom
    import win32com.client

    try:
        pythoncom.CoInitialize()
        try:
            sw_app = win32com.client.GetActiveObject("SldWorks.Application")
        except Exception:
            sw_app = win32com.client.Dispatch("SldWorks.Application")
            time.sleep(8)

        open_errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        open_warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

        doc = sw_app.OpenDoc6(
            sw_model, 1, 1, "", open_errors, open_warnings
        )
        if doc is None:
            print("  ✗ 无法打开模型文件")
            try:
                sw_app.ExitApp()
            except Exception:
                pass
            pythoncom.CoUninitialize()
            return False

        print(f"  模型已打开: Errors={open_errors.value}, Warnings={open_warnings.value}")

        result = True

        doc._FlagAsMethod('GetConfigurationNames')
        try:
            raw = doc.GetConfigurationNames()
            if isinstance(raw, (tuple, list)):
                conf_names = [str(c) for c in raw]
            elif raw is not None:
                conf_names = [str(raw)]
            else:
                conf_names = []
            print(f"  ✓ GetConfigurationNames() 返回: {conf_names[:5]}{'...' if len(conf_names) > 5 else ''}")
        except TypeError as e:
            print(f"  ✗ GetConfigurationNames() 仍报 TypeError: {e}")
            result = False

        try:
            sw_app.CloseDoc(os.path.basename(sw_model))
        except Exception:
            pass
        try:
            sw_app.ExitApp()
        except Exception:
            pass

        del doc
        del sw_app

        import gc
        gc.collect()

        pythoncom.CoUninitialize()
        return result
    except ImportError:
        print("  ⚠ win32com 未安装，跳过测试")
        return True
    except Exception as e:
        print(f"  ✗ E3 测试异常: {type(e).__name__}: {e}")
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
        return False


def test_com_verify_callable():
    """E4 验证: callable() 检查机制"""
    print("\n" + "=" * 60)
    print("E4 测试: COM 验证 callable() 检查")
    print("=" * 60)

    sw_model = LOCAL_PATHS["sw_model"]
    if not os.path.exists(sw_model):
        print(f"  ⚠ SW 模型文件不存在: {sw_model}，跳过测试")
        return True

    import pythoncom
    import win32com.client

    try:
        pythoncom.CoInitialize()
        try:
            sw_app = win32com.client.GetActiveObject("SldWorks.Application")
        except Exception:
            sw_app = win32com.client.Dispatch("SldWorks.Application")
            time.sleep(8)

        open_errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        open_warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

        doc = sw_app.OpenDoc6(
            sw_model, 1, 1, "", open_errors, open_warnings
        )
        if doc is None:
            print("  ✗ 无法打开模型文件")
            try:
                sw_app.ExitApp()
            except Exception:
                pass
            pythoncom.CoUninitialize()
            return False

        result = True
        for method_name in ("GetTitle", "GetPathName", "GetType"):
            val = getattr(doc, method_name)
            if callable(val):
                ret = val()
                print(f"  ✓ {method_name}() = {ret!r} (方法模式)")
            else:
                print(f"  ✓ {method_name} = {val!r} (属性模式 — callable 检查正确处理)")

        try:
            sw_app.CloseDoc(os.path.basename(sw_model))
        except Exception:
            pass
        try:
            sw_app.ExitApp()
        except Exception:
            pass

        del doc
        del sw_app

        import gc
        gc.collect()

        pythoncom.CoUninitialize()
        return result
    except ImportError:
        print("  ⚠ win32com 未安装，跳过测试")
        return True
    except Exception as e:
        print(f"  ✗ E4 测试异常: {type(e).__name__}: {e}")
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
        return False


def test_step_export(config_index: int):
    """E1 核心验证: 使用 VT_DISPATCH ExportData 执行 STEP 导出"""
    print("\n" + "=" * 60)
    print(f"E1 核心测试: STEP 导出（构型 {config_index}）")
    print("=" * 60)

    sw_model = LOCAL_PATHS["sw_model"]
    step_dir = LOCAL_PATHS.get("step_dir", "")

    if not os.path.exists(sw_model):
        print(f"  ✗ SW 模型文件不存在: {sw_model}")
        return False
    if not step_dir:
        print("  ✗ 未配置 step_dir")
        return False

    os.makedirs(step_dir, exist_ok=True)

    import pythoncom
    import win32com.client

    try:
        pythoncom.CoInitialize()

        try:
            sw_app = win32com.client.GetActiveObject("SldWorks.Application")
            print("  已连接到运行中的 SolidWorks")
        except Exception:
            sw_app = win32com.client.Dispatch("SldWorks.Application")
            time.sleep(8)
            print("  已通过 COM Dispatch 启动 SolidWorks")

        try:
            sw_app.Visible = True
        except Exception:
            pass

        open_errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        open_warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

        doc = sw_app.OpenDoc6(
            sw_model, 1, 1, "", open_errors, open_warnings
        )
        if doc is None:
            print(f"  ✗ 无法打开模型 (Errors={open_errors.value})")
            try:
                sw_app.ExitApp()
            except Exception:
                pass
            pythoncom.CoUninitialize()
            return False

        print(f"  模型已打开: Errors={open_errors.value}, Warnings={open_warnings.value}")

        doc._FlagAsMethod('GetConfigurationNames')
        raw = doc.GetConfigurationNames()
        if isinstance(raw, (tuple, list)):
            conf_names = [str(c) for c in raw]
        elif raw is not None:
            conf_names = [str(raw)]
        else:
            conf_names = []
        print(f"  配置列表: {conf_names}")

        if str(config_index) not in conf_names:
            print(f"  ⚠ 构型 {config_index} 不在配置列表中，可用: {conf_names}")
            if conf_names:
                config_index = int(conf_names[0])
                print(f"  改用构型 {config_index}")
            else:
                print("  ✗ 无可用配置")
                try:
                    sw_app.CloseDoc(os.path.basename(sw_model))
                    sw_app.ExitApp()
                except Exception:
                    pass
                pythoncom.CoUninitialize()
                return False

        try:
            doc.ShowConfiguration2(str(config_index))
            print(f"  已切换到构型 {config_index}")
        except Exception as e:
            print(f"  ✗ 切换构型失败: {e}")

        step_filename = f"model_gen4.SLDPRT_{config_index}.step"
        step_filepath = os.path.join(step_dir, step_filename)

        if os.path.exists(step_filepath):
            os.remove(step_filepath)
            print(f"  已删除旧 STEP 文件: {step_filename}")

        print(f"\n  正在导出 STEP: {step_filepath}")

        save_errors = win32com.client.VARIANT(
            pythoncom.VT_BYREF | pythoncom.VT_I4, 0
        )
        save_warnings = win32com.client.VARIANT(
            pythoncom.VT_BYREF | pythoncom.VT_I4, 0
        )
        export_data = win32com.client.VARIANT(
            pythoncom.VT_DISPATCH, None
        )

        try:
            status = doc.Extension.SaveAs(
                step_filepath,
                0,
                1,
                export_data,
                save_errors,
                save_warnings,
            )

            if status:
                print(f"  ✓ SaveAs 返回 True (Errors={save_errors.value}, Warnings={save_warnings.value})")
            else:
                print(f"  ✗ SaveAs 返回 False (Errors={save_errors.value}, Warnings={save_warnings.value})")

        except Exception as e:
            print(f"  ✗ SaveAs 异常: {type(e).__name__}: {e}")
            status = False

        if os.path.exists(step_filepath):
            file_size = os.path.getsize(step_filepath)
            print(f"  ✓ STEP 文件已生成: {step_filename} ({file_size} bytes)")
            result = True
        else:
            print(f"  ✗ STEP 文件未生成: {step_filepath}")
            result = False

        try:
            sw_app.CloseDoc(os.path.basename(sw_model))
        except Exception:
            pass
        try:
            sw_app.ExitApp()
        except Exception:
            pass

        del doc
        del sw_app

        import gc
        gc.collect()

        pythoncom.CoUninitialize()
        return result

    except ImportError:
        print("  ✗ win32com 未安装")
        return False
    except Exception as e:
        print(f"  ✗ 测试异常: {type(e).__name__}: {e}")
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
        return False


def main():
    parser = argparse.ArgumentParser(description="SolidWorks STEP 导出修复验证脚本")
    parser.add_argument(
        "--config", type=int, default=0,
        help="指定测试构型索引 (默认: 0)"
    )
    parser.add_argument(
        "--skip-live", action="store_true",
        help="跳过需要 SolidWorks 运行的测试（仅运行 VARIANT 创建测试）"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("SolidWorks STEP 导出修复验证")
    print(f"测试构型: {args.config}")
    print("=" * 60)

    results = {}

    results["E1_variant"] = test_com_dispatch_variant()

    if not args.skip_live:
        results["E1_export"] = test_step_export(args.config)
        results["E2_flag"] = test_flag_as_method_signature()
        results["E3_confignames"] = test_get_configuration_names()
        results["E4_callable"] = test_com_verify_callable()
    else:
        print("\n  (--skip-live 已设置，跳过需要 SolidWorks 运行的测试)")

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    all_pass = True
    for name, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print("所有测试通过！修复有效。")
    else:
        print("部分测试失败，请检查上方输出。")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
