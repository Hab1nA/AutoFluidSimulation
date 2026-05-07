"""
===============================================================================
诊断脚本 1: COM Extension 对象正确访问方式测试
测试 doc.Extension / ForceRebuildAll 在 pywin32 延迟绑定下的行为，
找到能正常调用的写法。

用法: python tools/diagnose_com_extension.py
===============================================================================
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import LOCAL_PATHS

SW_MODEL = LOCAL_PATHS["sw_model"]
STEP_DIR = LOCAL_PATHS.get("step_dir", "")


def diagnose():
    print("=" * 60)
    print("诊断: doc.Extension / ForceRebuildAll 的正确访问方式")
    print(f"模型: {os.path.basename(SW_MODEL)}")
    print("=" * 60)

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

    try:
        # ── 连接 SW ──
        print("\n[1] 连接 SolidWorks...")
        try:
            sw_app = win32com.client.GetActiveObject("SldWorks.Application")
            print("    ✓ 已连接运行中的 SW")
        except Exception:
            sw_app = win32com.client.Dispatch("SldWorks.Application")
            sw_app.Visible = True
            print("    ✓ 已启动新 SW 实例 (Dispatch)")
            time.sleep(8)

        try:
            rev = sw_app.RevisionNumber
            print(f"    SW 版本: {rev}")
        except Exception:
            pass

        # ── 打开模型 ──
        print("\n[2] 打开模型...")
        oe = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        ow = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        doc = sw_app.OpenDoc6(SW_MODEL, 1, 1, "", oe, ow)
        print(f"    OpenDoc6: Errors={oe.value}, Warnings={ow.value}")
        if doc is None:
            print("    FAIL: 无法打开模型")
            return 1
        print("    ✓ 模型已打开")

        # ── 测试 1: 直接属性访问 Extension ──
        print("\n[3] 测试 Extension 访问方式...")
        print("-" * 40)
        print("测试 A: doc.Extension (属性访问, 不加括号)")
        try:
            ext = doc.Extension
            print(f"    type(Extension) = {type(ext).__name__}")
            print(f"    repr(Extension) = {repr(ext)[:120]}")
            if isinstance(ext, bool):
                print("    ⚠ Extension 返回了 bool！这正是 ForceRebuildAll TypeError 的根因。")
        except Exception as e:
            print(f"    FAIL: {type(e).__name__}: {e}")

        print("\n测试 B: doc.Extension() (方法调用, 加括号)")
        try:
            ext = doc.Extension()
            print(f"    type(Extension()) = {type(ext).__name__}")
            print(f"    repr(Extension()) = {repr(ext)[:120]}")
            if isinstance(ext, bool):
                print("    ⚠ Extension() 也返回了 bool！")
        except Exception as e:
            print(f"    FAIL: {type(e).__name__}: {e}")

        print("\n测试 C: doc.GetExtension() (显式 getter 方法)")
        try:
            ext = doc.GetExtension()
            print(f"    type(GetExtension()) = {type(ext).__name__}")
            print(f"    repr(GetExtension()) = {repr(ext)[:120]}")
            if not isinstance(ext, bool) and ext is not None:
                print("    ✓ GetExtension() 返回了非 bool 对象！")
                # 进一步测试
                try:
                    params = ext.GetParameters()
                    print(f"    GetParameters() = {len(params) if params else 0} 个参数")
                except Exception as e:
                    print(f"    GetParameters 测试: {type(e).__name__}: {e}")
        except AttributeError:
            print("    GetExtension() 方法不存在")

        print("\n测试 D: _oleobj_ 底层 Invoke（最可靠但有代码侵入性）")
        try:
            import pywintypes
            # Extension 的 DISPID 通常是固定的, 但我们可以用 GetIDsOfNames
            names = ["Extension"]
            dispid = doc._oleobj_.GetIDsOfNames(0, names)
            # 同时获取 ForceRebuildAll 的 dispid
            ext_names = ["ForceRebuildAll"]
            ext_dispid = doc._oleobj_.GetIDsOfNames(0, ext_names)
            print(f"    Extension DISPID={dispid}")
            print(f"    ForceRebuildAll DISPID={ext_dispid}")
        except Exception as e:
            print(f"    FAIL: {type(e).__name__}: {e}")

        print("\n测试 E: 获取所有方法和属性的 DISPID 列表")
        try:
            # 尝试获取 IModelDoc2 的 Extension 属性并检测 ForceRebuildAll
            # 通过 _oleobj_ 直接 Invoke
            import pythoncom as pc
            dispid_ext = pc.DISPID_VALUE  # 先用默认值
            # 直接尝试调用 ForceRebuildAll
            try:
                ext2 = doc.Extension
                if not isinstance(ext2, bool):
                    ext2.ForceRebuildAll()
                    print("    ✓ ForceRebuildAll() 通过 Extension 属性调用成功！")
                else:
                    print("    ⚠ 跳过: Extension 是 bool")
            except TypeError as e:
                print(f"    TypeError (预期中的错误): {e}")
            except Exception as e:
                print(f"    其他错误: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"    FAIL: {type(e).__name__}: {e}")

        # ── 测试 2: 检测 ForceRebuildAll 是否存在 ──
        print("\n[4] 测试 ForceRebuildAll 可用性...")
        print("-" * 40)

        print("测试 F: _FlagAsMethod + Extension() 组合")
        try:
            doc._FlagAsMethod('Extension', True)
            ext3 = doc.Extension()
            print(f"    _FlagAsMethod 后 type = {type(ext3).__name__}")
            if not isinstance(ext3, bool) and ext3 is not None:
                try:
                    ext3.ForceRebuildAll()
                    print("    ✓ ForceRebuildAll() 调用成功！")
                except Exception as e:
                    print(f"    ForceRebuildAll 失败: {type(e).__name__}: {e}")
            else:
                print("    Extension 仍返回 bool")
        except Exception as e:
            print(f"    _FlagAsMethod 失败: {type(e).__name__}: {e}")

        # 注意：_FlagAsMethod 已持久化到对象，后续访问可能受影响
        # 这里先测试其他独立方案

        print("\n测试 G: win32com.client.Dispatch 包裹 Extension")
        try:
            raw_ext = doc.Extension
            if not isinstance(raw_ext, bool) and raw_ext is not None:
                ext4 = win32com.client.Dispatch(raw_ext)
                print(f"    Dispatch 包裹后 type = {type(ext4).__name__}")
                try:
                    ext4.ForceRebuildAll()
                    print("    ✓ ForceRebuildAll() 调用成功！")
                except Exception as e:
                    print(f"    ForceRebuildAll 失败: {type(e).__name__}: {e}")
            else:
                print(f"    Extension 返回了 {type(raw_ext).__name__}({raw_ext}), 无法 Dispatch 包裹")
        except Exception as e:
            print(f"    Dispatch 包裹失败: {type(e).__name__}: {e}")

        # ── 测试 3: 备选重建方案 ──
        print("\n[5] 测试备选重建方案...")
        print("-" * 40)

        print("测试 H: EditRebuild3 (当前构型重建)")
        try:
            doc.EditRebuild3()
            print("    ✓ EditRebuild3 成功")
        except Exception as e:
            print(f"    FAIL: {type(e).__name__}: {e}")

        print("\n测试 I: 获取配置列表 → 逐个切换并重建")
        try:
            raw = doc.GetConfigurationNames()
            if isinstance(raw, (tuple, list)):
                configs = [str(c) for c in raw]
            elif raw is not None:
                configs = [str(raw)]
            else:
                configs = []
            print(f"    配置数: {len(configs)}")
            if configs:
                first_cfg = configs[0]
                print(f"    测试配置 '{first_cfg}':")
                doc.ShowConfiguration2(first_cfg)
                print(f"      ShowConfiguration2 ✓")
                doc.EditRebuild3()
                print(f"      EditRebuild3 ✓")
        except Exception as e:
            print(f"    FAIL: {type(e).__name__}: {e}")

        # ── 总结 ──
        print("\n" + "=" * 60)
        print("诊断总结")
        print("=" * 60)
        print("请根据上述测试结果确定正确的 ForceRebuildAll 调用方式。")
        print("优先级:")
        print("  1. GetExtension().ForceRebuildAll() — 如果 GetExtension 可用")
        print("  2. _FlagAsMethod('Extension', True) + Extension().ForceRebuildAll()")
        print("  3. Dispatch 包裹 doc.Extension")
        print("  4. 降级为逐个配置 EditRebuild3()（最慢但最可靠）")

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
    sys.exit(diagnose())
