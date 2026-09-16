using LibreHardwareMonitor.Hardware;

namespace SensortapHelper;

/// <summary>
/// Thin wrapper around LibreHardwareMonitorLib's <see cref="Computer"/> root
/// object. Owns enumeration and lookup of temperature/fan/voltage/clock/load
/// sensors.
///
/// CONFIDENCE NOTE (flagged for verification once a real .NET 8 SDK is
/// available — this library's exact surface was not exercised against a
/// live build in this environment):
///
/// The class names and property names below (Computer, IsCpuEnabled,
/// IsGpuEnabled, IsMotherboardEnabled, IsMemoryEnabled, IsStorageEnabled,
/// IsNetworkEnabled, IsControllerEnabled, Open(), Hardware, SubHardware,
/// Sensors, SensorType, Identifier, Name, HardwareType, Value, Min, Max,
/// and IVisitor/Update()) reflect the LibreHardwareMonitorLib API as
/// documented in its public GitHub repository and NuGet package README at
/// the time of writing. They are believed correct but have NOT been
/// compiled against the real package in this environment. Verify against
/// the actual installed package version's source/XML docs before trusting
/// this in CI or production (task 9.5).
///
/// DRIVER INSTALLATION (Req 15.5 / 15.4): LibreHardwareMonitorLib's Ring0
/// access (the WinRing0/inpoutx64-family kernel driver used for MSR/PCI
/// reads on some sensors) is installed and started internally by the
/// library's Ring0 helper class when <see cref="Computer.Open"/> is called,
/// UNLESS that mechanism is disabled. As of the versions of the library
/// documented publicly, there is no longer a simple public boolean flag on
/// Computer purely named "InstallDriver" in all releases; instead:
///
///   1. The library only attempts driver installation/loading when a
///      feature that needs Ring0 is enabled (broadly: CPU MSR reads on
///      some vendors, and some motherboard/EC access paths). Superficial
///      sensors (many CPU package/core temps via defined MSRs, GPU sensors
///      via vendor APIs, storage SMART, memory) do not require it on most
///      hardware.
///   2. This helper deliberately enables the *minimum* hardware groups
///      needed for temperature/fan/voltage/clock/load coverage and leaves
///      IsMotherboardEnabled = false by default, since motherboard/Super-IO
///      access is the group most likely to trigger Ring0 driver loading
///      for embedded-controller and Super-IO chip reads.
///   3. Ring0 acquisition in LibreHardwareMonitorLib, when triggered, will
///      use an ALREADY-INSTALLED/registered driver service if one exists
///      under its expected service name, and otherwise attempts to install
///      one. sensortap's contractual requirement (Req 15.5) is that it must
///      never install/register/start a kernel driver. Because the library
///      does not expose (as far as could be verified without a live SDK) a
///      single documented "never install, but use if present" flag common
///      across all recent versions, this wrapper takes the conservative
///      route of NOT enabling Motherboard/Super-IO monitoring at all, so
///      the driver-installation code path inside the library is never
///      reached from this helper. Temperature/fan/voltage/clock/load
///      sensors that are only reachable via that path will simply not
///      appear in this helper's enumeration, which is consistent with
///      Req 15.5's fallback behavior of treating shim-dependent sensors as
///      unavailable rather than installing anything.
///   4. Whoever first builds this against a real SDK MUST verify, by
///      inspecting the installed LibreHardwareMonitorLib version's
///      Ring0.cs / Computer.cs source, whether:
///        a) there is a supported way to explicitly disable driver
///           installation while still reading from an already-running
///           driver service, and if so, wire that flag in explicitly here
///           (this is the ideal outcome, since it would let Req 13.12/15.5
///           "use one only when already present" be satisfied more fully
///           for motherboard sensors too), or
///        b) the conservative "leave Motherboard disabled" approach in
///           this file remains the only safe option for that version.
///      This is the single biggest LibreHardwareMonitorLib-API-surface
///      uncertainty in this task and is called out again in
///      helper_src/README.md.
/// </summary>
public sealed class HardwareMonitor : IDisposable
{
    private static readonly HashSet<SensorType> WantedTypes = new()
    {
        SensorType.Temperature,
        SensorType.Fan,
        SensorType.Voltage,
        SensorType.Clock,
        SensorType.Load,
    };

    private readonly Computer _computer;
    private readonly UpdateVisitor _updateVisitor = new();
    private bool _opened;

    public HardwareMonitor()
    {
        // Only enable hardware groups that commonly expose our five wanted
        // SensorTypes without requiring the library's Ring0 driver-install
        // path. See the class-level comment above for the reasoning and
        // the follow-up verification this needs.
        _computer = new Computer
        {
            IsCpuEnabled = true,
            IsGpuEnabled = true,
            IsMemoryEnabled = true,
            IsStorageEnabled = true,
            IsNetworkEnabled = false,
            IsMotherboardEnabled = false, // deliberately off -- see class doc
            IsControllerEnabled = false,
            IsBatteryEnabled = false,
            IsPsuEnabled = false,
        };
    }

    public void Open()
    {
        if (_opened)
        {
            return;
        }

        _computer.Open();
        _opened = true;
    }

    /// <summary>
    /// Returns metadata + current value for every Temperature/Fan/Voltage/
    /// Clock/Load sensor currently known to the library, across every
    /// Hardware node and every level of SubHardware, with no cap imposed
    /// below 512 sensors per SensorType (Req 13.5). There is no artificial
    /// limit at all in this implementation -- every sensor the library
    /// reports is returned.
    /// </summary>
    public List<SensorRecord> List()
    {
        EnsureOpen();
        RefreshAll();

        var results = new List<SensorRecord>();
        foreach (var hardware in _computer.Hardware)
        {
            CollectFromHardware(hardware, results);
        }

        return results;
    }

    /// <summary>
    /// Returns current values for exactly the requested sensor ids, in the
    /// order the caller listed them. Unknown ids are simply omitted from
    /// the result rather than raising -- the Python-side bridge (task 9.4)
    /// is expected to reconcile against its own last-known id set.
    /// </summary>
    public List<SensorRecord> Read(IReadOnlyCollection<string> ids)
    {
        EnsureOpen();
        RefreshAll();

        var wanted = new HashSet<string>(ids);
        var all = new List<SensorRecord>();
        foreach (var hardware in _computer.Hardware)
        {
            CollectFromHardware(hardware, all);
        }

        var byId = new Dictionary<string, SensorRecord>();
        foreach (var record in all)
        {
            byId[record.Id] = record;
        }

        var results = new List<SensorRecord>();
        foreach (var id in ids)
        {
            if (byId.TryGetValue(id, out var record))
            {
                results.Add(record);
            }
        }

        return results;
    }

    private void EnsureOpen()
    {
        if (!_opened)
        {
            Open();
        }
    }

    private void RefreshAll()
    {
        // Computer.Accept(IVisitor) is the documented way to force every
        // Hardware node (and its SubHardware) to refresh sensor values
        // before reading IHardware.Sensors. Without this, Sensor.Value can
        // be stale or null on some backends.
        _computer.Accept(_updateVisitor);
    }

    private static void CollectFromHardware(IHardware hardware, List<SensorRecord> results)
    {
        hardware.Update();

        foreach (var sensor in hardware.Sensors)
        {
            if (!WantedTypes.Contains(sensor.SensorType))
            {
                continue;
            }

            results.Add(new SensorRecord
            {
                Id = BuildSensorId(hardware, sensor),
                HardwareId = hardware.Identifier?.ToString() ?? "",
                HardwareName = hardware.Name ?? "",
                HardwareType = hardware.HardwareType.ToString(),
                SensorType = sensor.SensorType.ToString(),
                Name = sensor.Name ?? "",
                Value = sensor.Value,
                Min = sensor.Min,
                Max = sensor.Max,
            });
        }

        foreach (var sub in hardware.SubHardware)
        {
            CollectFromHardware(sub, results);
        }
    }

    /// <summary>
    /// Builds a helper-process-local identifier for one sensor. Stable
    /// within this process's lifetime (same hardware id + sensor type +
    /// sensor identifier string always yields the same value), which is
    /// all "read ids" round-tripping requires. The Python bridge (task 9.4)
    /// derives the real, hashed, cross-run-stable Sensor_Id itself from
    /// HardwareId/Name/SensorType -- this is deliberately NOT that value.
    /// </summary>
    private static string BuildSensorId(IHardware hardware, ISensor sensor)
    {
        // ISensor.Identifier is LibreHardwareMonitorLib's own internal
        // identifier (e.g. "/amdcpu/0/temperature/0") and is already
        // unique and stable within one process/run, so it is used as-is.
        return sensor.Identifier?.ToString()
               ?? $"{hardware.Identifier}/{sensor.SensorType}/{sensor.Name}";
    }

    public void Dispose()
    {
        if (_opened)
        {
            _computer.Close();
            _opened = false;
        }
    }

    /// <summary>
    /// Visits every hardware node and sub-node so their sensors refresh.
    /// This mirrors the pattern shown in LibreHardwareMonitorLib's own
    /// sample console app (per its GitHub README) -- again, not verified
    /// by compiling here.
    /// </summary>
    private sealed class UpdateVisitor : IVisitor
    {
        public void VisitComputer(IComputer computer) => computer.Traverse(this);

        public void VisitHardware(IHardware hardware)
        {
            hardware.Update();
            foreach (var sub in hardware.SubHardware)
            {
                sub.Accept(this);
            }
        }

        public void VisitSensor(ISensor sensor) { }

        public void VisitParameter(IParameter parameter) { }
    }
}
