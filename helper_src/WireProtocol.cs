using System.Text.Json.Serialization;

namespace SensortapHelper;

/// <summary>
/// Newline-delimited JSON request shape sent by the Python side over the
/// named pipe, per design.md's "Wire format" section.
///
///   {"cmd": "list"}
///   {"cmd": "read", "ids": ["cpu-temp-0", "gpu-fan-1"]}
///   {"cmd": "ping"}
///   {"cmd": "shutdown"}
///
/// All four commands share one shape; "ids" is only present/meaningful for
/// "read". System.Text.Json ignores properties that are absent from the
/// incoming JSON, and null/absent are treated identically for "Ids" here.
/// </summary>
public sealed class HelperRequest
{
    [JsonPropertyName("cmd")]
    public string Cmd { get; set; } = "";

    [JsonPropertyName("ids")]
    public List<string>? Ids { get; set; }
}

/// <summary>
/// Newline-delimited JSON response shape written back by the helper.
///
/// Success:   {"ok": true, "sensors": [...]}
/// Failure:   {"ok": false, "error": "..."}
///
/// "sensors" is populated for "list" and "read"; it is omitted (null) for
/// "ping" and "shutdown" acknowledgements, which only need "ok".
/// </summary>
public sealed class HelperResponse
{
    [JsonPropertyName("ok")]
    public bool Ok { get; set; }

    [JsonPropertyName("sensors")]
    public List<SensorRecord>? Sensors { get; set; }

    [JsonPropertyName("error")]
    public string? Error { get; set; }

    public static HelperResponse Success(List<SensorRecord>? sensors = null) =>
        new() { Ok = true, Sensors = sensors };

    public static HelperResponse Failure(string error) =>
        new() { Ok = false, Error = error };
}

/// <summary>
/// One sensor's metadata + current value, as reported by the helper.
///
/// Field choices are meant to be easy for a future
/// adapters/windows/hwmon_bridge.py (task 9.4) to map onto SensorInfo /
/// Reading without guesswork:
///
///   - Id: a stable-for-this-process string built from hardware identity +
///     sensor type + sensor index (see HardwareMonitor.BuildSensorId). This
///     is NOT the final Sensor_Id the Python core assigns -- task 9.4 is
///     responsible for hashing/deriving the real Sensor_Id from persistent
///     hardware identifiers. This Id only needs to be stable *within one
///     helper process lifetime* so that "read ids" round-trips correctly.
///   - HardwareId: a LibreHardwareMonitorLib-reported identifier string for
///     the parent hardware node (motherboard, CPU, GPU, etc.), included so
///     the bridge can build a real persistent hardware identifier.
///   - HardwareName / HardwareType: human-readable parent hardware info.
///   - SensorType: one of "Temperature", "Fan", "Voltage", "Clock", "Load"
///     (the LibreHardwareMonitorLib SensorType enum name, verbatim).
///   - Name: the sensor's own display name as LibreHardwareMonitorLib
///     reports it (e.g. "CPU Package", "Fan #1").
///   - Value / Min / Max: current/observed extremes, null when not yet
///     sampled by the library.
/// </summary>
public sealed class SensorRecord
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = "";

    [JsonPropertyName("hardware_id")]
    public string HardwareId { get; set; } = "";

    [JsonPropertyName("hardware_name")]
    public string HardwareName { get; set; } = "";

    [JsonPropertyName("hardware_type")]
    public string HardwareType { get; set; } = "";

    [JsonPropertyName("sensor_type")]
    public string SensorType { get; set; } = "";

    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("value")]
    public float? Value { get; set; }

    [JsonPropertyName("min")]
    public float? Min { get; set; }

    [JsonPropertyName("max")]
    public float? Max { get; set; }
}
