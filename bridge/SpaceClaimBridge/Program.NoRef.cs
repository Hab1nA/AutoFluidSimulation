using System;
using System.Collections;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

namespace AutoFluidSimulation.Bridge
{
    /// <summary>
    /// SpaceClaim Bridge — C# 5 兼容版本（纯 COM 互操作，无需 SpaceClaim API 引用）
    ///
    /// 使用 .NET Framework 4.x 自带的 csc.exe 即可编译。
    ///
    /// 用法:
    ///   SpaceClaimBridge.exe --script &lt;transit.py&gt; --config &lt;N&gt; --stepdir &lt;dir&gt; --scdocdir &lt;dir&gt; [--timeout &lt;s&gt;]
    ///
    /// 返回值: 0=成功 1=脚本失败 2=连接失败 3=输出验证失败 4=参数错误 5=超时
    /// </summary>
    class Program
    {
        private static readonly string[] SpaceClaimProgIds =
        {
            "SpaceClaim.Application",
            "SpaceClaim.Application.V23",
            "SCDM.Application",
        };

        static int Main(string[] args)
        {
            try
            {
                Options opts = ParseArgs(args);
                if (opts == null) return 4;
                return Execute(opts);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[BRIDGE_FATAL] " + ex.ToString());
                return 1;
            }
        }

        class Options
        {
            public string Script, Config, StepDir, ScdocDir;
            public int Timeout = 300;
        }

        static Options ParseArgs(string[] args)
        {
            Options o = new Options();
            for (int i = 0; i < args.Length; i++)
            {
                switch (args[i].ToLowerInvariant())
                {
                    case "--script":
                        if (++i < args.Length) o.Script = args[i];
                        break;
                    case "--config":
                        if (++i < args.Length) o.Config = args[i];
                        break;
                    case "--stepdir":
                        if (++i < args.Length) o.StepDir = args[i];
                        break;
                    case "--scdocdir":
                        if (++i < args.Length) o.ScdocDir = args[i];
                        break;
                    case "--timeout":
                        if (++i < args.Length)
                        {
                            int t;
                            if (int.TryParse(args[i], out t)) o.Timeout = t;
                        }
                        break;
                    default:
                        Console.Error.WriteLine("未知参数: " + args[i]);
                        Console.Error.WriteLine("用法: SpaceClaimBridge.exe --script <path> --config <n> --stepdir <dir> --scdocdir <dir> [--timeout <s>]");
                        return null;
                }
            }
            if (string.IsNullOrEmpty(o.Script) || string.IsNullOrEmpty(o.Config) ||
                string.IsNullOrEmpty(o.StepDir) || string.IsNullOrEmpty(o.ScdocDir))
            {
                Console.Error.WriteLine("缺少必要参数");
                return null;
            }
            return o;
        }

        static int Execute(Options o)
        {
            Console.WriteLine(string.Format("[BRIDGE] script={0} config={1}", o.Script, o.Config));
            Console.WriteLine(string.Format("[BRIDGE] stepdir={0} scdocdir={1}", o.StepDir, o.ScdocDir));

            string stepFile = Path.Combine(o.StepDir, "model_gen4.SLDPRT_" + o.Config + ".step");
            if (!File.Exists(stepFile))
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] STEP file not found: " + stepFile);
                return 3;
            }
            Console.WriteLine(string.Format("[BRIDGE] STEP: {0} ({1} B)", stepFile, new FileInfo(stepFile).Length));

            Directory.CreateDirectory(o.ScdocDir);

            dynamic app = ConnectSC();
            if (app == null) return 2;

            Hashtable ps = new Hashtable();
            ps["config_name"] = o.Config;
            ps["step_dir"]    = o.StepDir;
            ps["scdoc_dir"]   = o.ScdocDir;

            Console.WriteLine("[BRIDGE] Running RunScript...");
            Stopwatch sw = Stopwatch.StartNew();
            try
            {
                app.RunScript(o.Script, ps);
                sw.Stop();
                Console.WriteLine(string.Format("[BRIDGE] RunScript returned (elapsed {0:F1}s)", sw.Elapsed.TotalSeconds));
            }
            catch (Exception ex)
            {
                sw.Stop();
                COMException comEx = ex as COMException;
                if (comEx != null && (ex.Message.Contains("RPC") || ex.Message.Contains("disconnected")))
                {
                    Console.WriteLine("[BRIDGE] SC COM disconnected (script may have exited normally)");
                }
                else
                {
                    Console.Error.WriteLine(string.Format("[BRIDGE_ERROR] RunScript exception: {0}: {1}", ex.GetType().Name, ex.Message));
                    if (sw.Elapsed.TotalSeconds > o.Timeout)
                        return 5;
                    return 1;
                }
            }

            string scdocFile = Path.Combine(o.ScdocDir, "model_gen4_" + o.Config + ".scdoc");
            if (File.Exists(scdocFile))
            {
                FileInfo fi = new FileInfo(scdocFile);
                Console.WriteLine(string.Format("[BRIDGE] OK SCDOC: {0} ({1} B)", scdocFile, fi.Length));
                return 0;
            }

            Console.Error.WriteLine("[BRIDGE_ERROR] SCDOC not found: " + scdocFile);
            return 3;
        }

        static object ConnectSC()
        {
            foreach (string pid in SpaceClaimProgIds)
            {
                try
                {
                    object app = Marshal.GetActiveObject(pid);
                    Console.WriteLine("[BRIDGE] Connected to SpaceClaim (ProgID=" + pid + ")");
                    return app;
                }
                catch (COMException) { continue; }
            }

            string[] exePaths = {
                @"C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe",
                @"C:\Program Files\ANSYS Inc\v232\SCDM\SpaceClaim.exe",
                @"C:\Program Files\ANSYS Inc\v241\SCDM\SpaceClaim.exe",
            };
            string scExe = null;
            foreach (string p in exePaths) { if (File.Exists(p)) { scExe = p; break; } }
            if (scExe == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim.exe not found");
                return null;
            }

            Console.WriteLine("[BRIDGE] Starting SpaceClaim: " + scExe);
            try { Process.Start(scExe); }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] Failed to start: " + ex.Message);
                return null;
            }

            Console.WriteLine("[BRIDGE] Waiting for COM...");
            for (int i = 0; i < 120; i++)
            {
                Thread.Sleep(1000);
                foreach (string pid in SpaceClaimProgIds)
                {
                    try
                    {
                        object app = Marshal.GetActiveObject(pid);
                        Console.WriteLine(string.Format("[BRIDGE] COM ready ({0}s, ProgID={1})", i + 1, pid));
                        Thread.Sleep(3000);
                        return app;
                    }
                    catch (COMException) { continue; }
                }
            }
            Console.Error.WriteLine("[BRIDGE_ERROR] COM wait timeout");
            return null;
        }
    }
}
