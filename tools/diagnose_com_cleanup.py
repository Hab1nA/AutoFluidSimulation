"""
===============================================================================
诊断脚本 3: COM 资源清理与重连稳定性测试
模拟 SW 自动化中的"启动→操作→退出→再启动"循环，
测试 COM 资源是否正确释放，第二次 Dispatch 是否退化。

用法: python tools/diagnose_com_cleanup.py
===============================================================================
"""
import os
import sys
import time
import gc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import LOCAL_PATHS

SW_MODEL = LOCAL_PATHS["sw_model"]


def test_single_cycle(cycle_num: int) -> dict:
    """执行一次完整的 SW 自动化周期，返回诊断结果。"""
    result = {
        "cycle": cycle_num,
        "connect_ok": False,
        "open_ok": False,
        "get_title_ok": False,
        "get_configs_ok": False,
        "close_ok": False,
        "exit_ok": False,
        "errors": [],
    }

    try:
        import win32com.client
        import pythoncom
    except ImportError as e:
        result["errors"].append(f"pywin32 import: {e}")
        return result

    pythoncom.CoInitialize()

    sw_app = None
    doc = None

    try:
        # ── 连接 ──
        try:
            sw_app = win32com.client.GetActiveObject("SldWorks.Application")
        except Exception:
            sw_app = win32com.client.Dispatch("SldWorks.Application")
            sw_app.Visible = True
            time.sleep(8)
        result["connect_ok"] = sw_app is not None

        if not sw_app:
            result["errors"].append("无法连接 SW")
            return result

        # ── 验证 SW COM 对象完整性 ──
        try:
            rev = sw_app.RevisionNumber
        except Exception as e:
            result["errors"].append(f"RevisionNumber 不可用: {type(e).__name__}")

        # ── 打开模型 ──
        oe = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        ow = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        try:
            doc = sw_app.OpenDoc6(SW_MODEL, 1, 1, "", oe, ow)
            result["open_ok"] = doc is not None
        except Exception as e:
            result["errors"].append(f"OpenDoc6: {type(e).__name__}: {e}")
            return result

        if not doc:
            result["errors"].append("OpenDoc6 返回 None")
            return result

        # ── 验证 doc COM 对象 ──
        try:
            title = doc.GetTitle()
            result["get_title_ok"] = bool(title)
        except Exception as e:
            result["errors"].append(f"GetTitle: {type(e).__name__}: {e}")

        try:
            raw = doc.GetConfigurationNames()
            result["get_configs_ok"] = raw is not None
        except Exception as e:
            result["errors"].append(f"GetConfigurationNames: {type(e).__name__}: {e}")

        # ── 关闭模型 ──
        try:
            sw_app.CloseDoc(os.path.basename(SW_MODEL))
            result["close_ok"] = True
        except Exception as e:
            result["errors"].append(f"CloseDoc: {type(e).__name__}: {e}")

        # ── 退出 SW ──
        try:
            sw_app.ExitApp()
            result["exit_ok"] = True
        except Exception as e:
            result["errors"].append(f"ExitApp: {type(e).__name__}: {e}")

    finally:
        # ── COM 资源释放 ──
        doc = None
        sw_app = None
        gc.collect()
        for _ in range(2):
            try:
                pythoncom.CoFreeUnusedLibraries()
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

    return result


def wait_sw_exit(timeout: int = 20) -> bool:
    """等待 SolidWorks 进程完全退出。"""
    import subprocess
    for _ in range(timeout):
        time.sleep(1)
        try:
            result = subprocess.run(
                ["tasklist", "/fi", "IMAGENAME eq SLDWORKS.exe",
                 "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=5,
            )
            if "SLDWORKS.exe" not in result.stdout:
                return True
        except Exception:
            pass
    return False


def main():
    print("=" * 60)
    print("COM 资源清理与重连稳定性测试")
    print(f"模型: {os.path.basename(SW_MODEL)}")
    print("=" * 60)

    NUM_CYCLES = 2  # 测试 2 轮启动→退出

    for cycle in range(1, NUM_CYCLES + 1):
        print(f"\n{'─' * 40}")
        print(f"第 {cycle}/{NUM_CYCLES} 轮")
        print(f"{'─' * 40}")

        result = test_single_cycle(cycle)

        # 打印结果
        checks = [
            ("连接 SW", result["connect_ok"]),
            ("打开模型", result["open_ok"]),
            ("GetTitle", result["get_title_ok"]),
            ("配置列表", result["get_configs_ok"]),
            ("关闭模型", result["close_ok"]),
            ("退出 SW", result["exit_ok"]),
        ]
        for label, ok in checks:
            status = "✓" if ok else "✗"
            print(f"  {status} {label}")

        if result["errors"]:
            print(f"  ⚠ 错误:")
            for err in result["errors"]:
                print(f"      {err}")

        all_ok = all(v for _, v in checks)
        if all_ok:
            print(f"  第{cycle}轮: 全部通过 ✓")
        else:
            print(f"  第{cycle}轮: 存在失败 ✗")

        # ── 等待 SW 进程退出后再下一轮 ──
        if cycle < NUM_CYCLES:
            print(f"\n  等待 SW 进程完全退出...")
            exited = wait_sw_exit(20)
            if exited:
                print(f"  ✓ SW 进程已退出")
            else:
                print(f"  ⚠ SW 未在 20s 内退出，可能残留，强制清理...")
                import subprocess
                try:
                    subprocess.run(
                        ["taskkill", "/f", "/im", "SLDWORKS.exe"],
                        capture_output=True, timeout=10,
                    )
                    time.sleep(3)
                except Exception:
                    pass

            # 额外冷却
            print(f"  冷却 5 秒后进入下一轮...")
            time.sleep(5)

    # ── 总结 ──
    print("\n" + "=" * 60)
    print("测试结论")
    print("=" * 60)
    print("如果第2轮出现 AttributeError (如 GetMacroMethods/OpenDoc6 不可用),")
    print("说明 COM 清理不充分，需要在:")
    print("  1. sw_app/del 后增加 gc.collect()")
    print("  2. CoFreeUnusedLibraries() 调用两次")
    print("  3. 增加 SW 进程退出等待时间")
    print("  4. 或增加 taskkill 强制清理")
    print()
    print("如果两轮都通过 — COM 资源管理逻辑可以放心使用。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
