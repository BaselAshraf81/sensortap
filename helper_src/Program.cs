using System.IO.Pipes;
using System.Text;
using System.Text.Json;

namespace SensortapHelper;

/// <summary>
/// Entry point for the sensortap Windows helper.
///
/// Roles, per the 8.2 decision recorded in tasks.md: Python is the named
/// pipe SERVER (it creates the pipe with a per-user security descriptor and
/// PIPE_REJECT_REMOTE_CLIENTS). This helper is the pipe CLIENT -- it
/// connects out to a pipe that already exists by the time this process is
/// launched.
///
/// Invocation contract (decided here, since something has to tell the
/// helper which pipe to connect to, and a command-line argument is the
/// simplest, most debuggable option):
///
///     sensortap-helper.exe <pipe-name>
///
/// where <pipe-name> is the short pipe name Python chose when it called
/// CreateNamedPipe (e.g. "sensortap-<random>"), WITHOUT the "\\.\pipe\"
/// prefix -- NamedPipeClientStream adds that prefix itself when given "."
/// as the server name.
///
/// Token handshake (per design.md's "Launch_Token handshake" section and
/// task 8.2's decision):
///   1. Python spawns this process with the pipe name as argv[0], and
///      writes the Launch_Token as a single line to this process's stdin,
///      then closes stdin.
///   2. This process reads that one line from stdin BEFORE attempting to
///      connect to the pipe, so the token is captured even if the pipe
///      connect takes a moment.
///   3. Once connected, this process's first outgoing message is
///      "HELLO {token}" as one line of newline-delimited text (not JSON --
///      the HELLO line is a bare handshake line per design.md; all
///      messages after the handshake are newline-delimited JSON).
///   4. If Python's server-side authentication rejects the token, Python
///      closes the pipe immediately without sending any response. This
///      helper detects that as an IOException/EndOfStream on its next read
///      and exits with a non-zero code without retrying -- per Req 13.6,
///      "exit on any authentication failure".
/// </summary>
public static class Program
{
    private const int AuthFailureExitCode = 1;
    private const int UsageErrorExitCode = 2;
    private const int UnexpectedErrorExitCode = 3;

    public static int Main(string[] args)
    {
        if (args.Length < 1 || string.IsNullOrWhiteSpace(args[0]))
        {
            Console.Error.WriteLine("usage: sensortap-helper.exe <pipe-name>");
            return UsageErrorExitCode;
        }

        var pipeName = args[0];

        string? token;
        try
        {
            // Read exactly one line from stdin. Python writes the token
            // followed by a newline and then closes stdin, per the 8.2/9.1
            // contract ("Read the Launch_Token from stdin").
            token = Console.In.ReadLine();
        }
        catch (IOException ex)
        {
            Console.Error.WriteLine($"failed to read Launch_Token from stdin: {ex.Message}");
            return UnexpectedErrorExitCode;
        }

        if (string.IsNullOrEmpty(token))
        {
            Console.Error.WriteLine("no Launch_Token received on stdin");
            return AuthFailureExitCode;
        }

        using var pipe = new NamedPipeClientStream(
            serverName: ".",
            pipeName: pipeName,
            direction: PipeDirection.InOut,
            options: PipeOptions.Asynchronous);

        try
        {
            // Bounded connect attempt. Python is expected to already have
            // the pipe instance created and waiting before it launches this
            // process, but a short timeout guards against a spawn/connect
            // race without hanging forever if something goes wrong.
            pipe.Connect(timeout: 10_000);
        }
        catch (Exception ex) when (ex is IOException or TimeoutException or UnauthorizedAccessException)
        {
            Console.Error.WriteLine($"failed to connect to pipe '{pipeName}': {ex.Message}");
            return AuthFailureExitCode;
        }

        // IMPORTANT: even `new UTF8Encoding(encoderShouldEmitUTF8Identifier:
        // false)` was observed to still prepend a BOM (EF BB BF) on the
        // pipe's first write when handed to StreamWriter over a
        // NamedPipeClientStream in this runtime -- confirmed by capturing
        // the actual bytes Python received. Rather than fight
        // StreamWriter's preamble behaviour further, the HELLO line is
        // written as raw bytes directly to the pipe stream, bypassing
        // StreamWriter entirely for this one line. StreamReader/StreamWriter
        // (also BOM-free UTF-8) are still used for every message after the
        // handshake, where no external byte-exact comparison is performed.
        var utf8NoBom = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false);
        try
        {
            byte[] helloBytes = utf8NoBom.GetBytes($"HELLO {token}\n");
            pipe.Write(helloBytes, 0, helloBytes.Length);
            pipe.Flush();
        }
        catch (IOException ex)
        {
            Console.Error.WriteLine($"failed to send HELLO: {ex.Message}");
            return AuthFailureExitCode;
        }

        using var reader = new StreamReader(pipe, utf8NoBom, detectEncodingFromByteOrderMarks: false, leaveOpen: true);
        using var writer = new StreamWriter(pipe, utf8NoBom, leaveOpen: true) { AutoFlush = true, NewLine = "\n" };

        // Per design.md: "Mismatch, or any other first message, closes the
        // pipe and terminates the child immediately." From this helper's
        // side, that surfaces as the pipe closing before or instead of any
        // further traffic. There is no explicit "AUTH_OK" message defined
        // in the wire format described in tasks.md/design.md beyond the
        // ordinary request/response loop, so authentication success is
        // implicit: if the pipe stays open and we can read a well-formed
        // request line, we proceed. If the pipe is closed immediately
        // (ReadLine returns null / throws), that is treated as an auth
        // failure per Req 13.6 ("exit on any authentication failure").
        HardwareMonitor? monitor = null;
        try
        {
            monitor = new HardwareMonitor();
            monitor.Open();

            return RunLoop(reader, writer, monitor);
        }
        catch (IOException ex)
        {
            // The pipe closing unexpectedly right after HELLO, before any
            // request was ever read, is indistinguishable from an
            // authentication rejection at this layer -- treat it as one.
            Console.Error.WriteLine($"pipe closed: {ex.Message}");
            return AuthFailureExitCode;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"unexpected error: {ex.Message}");
            return UnexpectedErrorExitCode;
        }
        finally
        {
            monitor?.Dispose();
        }
    }

    /// <summary>
    /// Reads newline-delimited JSON requests from the pipe and writes
    /// newline-delimited JSON responses back, until "shutdown" is received
    /// or the pipe closes. Returns the process exit code.
    /// </summary>
    private static int RunLoop(StreamReader reader, StreamWriter writer, HardwareMonitor monitor)
    {
        while (true)
        {
            string? line = reader.ReadLine();
            if (line is null)
            {
                // Pipe closed by the server side (Python). This is a
                // normal way for the connection to end (e.g. Python process
                // exiting without sending an explicit "shutdown"), so this
                // is treated as a clean exit rather than an error, UNLESS
                // it happens before any request was ever successfully
                // processed -- but distinguishing that here would require
                // extra state for little benefit; Python's shutdown path
                // (per design.md's Lifecycle section) always sends an
                // explicit "shutdown" command first, so reaching EOF here
                // during normal operation is expected only after that.
                return 0;
            }

            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            HelperRequest? request;
            try
            {
                request = JsonSerializer.Deserialize<HelperRequest>(line);
            }
            catch (JsonException ex)
            {
                WriteResponse(writer, HelperResponse.Failure($"malformed request: {ex.Message}"));
                continue;
            }

            if (request is null || string.IsNullOrEmpty(request.Cmd))
            {
                WriteResponse(writer, HelperResponse.Failure("missing 'cmd' field"));
                continue;
            }

            switch (request.Cmd)
            {
                case "list":
                    HandleList(writer, monitor);
                    break;

                case "read":
                    HandleRead(writer, monitor, request.Ids);
                    break;

                case "ping":
                    WriteResponse(writer, HelperResponse.Success());
                    break;

                case "shutdown":
                    WriteResponse(writer, HelperResponse.Success());
                    return 0;

                default:
                    WriteResponse(writer, HelperResponse.Failure($"unknown cmd '{request.Cmd}'"));
                    break;
            }
        }
    }

    private static void HandleList(StreamWriter writer, HardwareMonitor monitor)
    {
        try
        {
            var sensors = monitor.List();
            WriteResponse(writer, HelperResponse.Success(sensors));
        }
        catch (Exception ex)
        {
            WriteResponse(writer, HelperResponse.Failure($"list failed: {ex.Message}"));
        }
    }

    private static void HandleRead(StreamWriter writer, HardwareMonitor monitor, List<string>? ids)
    {
        if (ids is null || ids.Count == 0)
        {
            WriteResponse(writer, HelperResponse.Failure("'read' requires a non-empty 'ids' list"));
            return;
        }

        try
        {
            var sensors = monitor.Read(ids);
            WriteResponse(writer, HelperResponse.Success(sensors));
        }
        catch (Exception ex)
        {
            WriteResponse(writer, HelperResponse.Failure($"read failed: {ex.Message}"));
        }
    }

    private static void WriteResponse(StreamWriter writer, HelperResponse response)
    {
        var json = JsonSerializer.Serialize(response);
        writer.WriteLine(json);
    }
}
