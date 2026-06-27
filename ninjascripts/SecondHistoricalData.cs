#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.IO;
using System.Windows.Media;
using System.Windows.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class SecondHistoricalData : Strategy
    {
        private string filePath;
        private string statusPath;
        private EMA ema21;
        private EMA ema75;
        private EMA ema150;
        private Stochastics stochastic;

        // --- Connection / heartbeat hardening ---------------------------------
        // OnBarUpdate only fires when bars flow, so a dropped feed makes the
        // strategy go silent and the CSV freezes with no signal of *why*. We
        // subscribe to the Connection status and run a UI-thread DispatcherTimer
        // that writes a heartbeat status file every HeartbeatSeconds regardless
        // of bar flow. Python reads it to tell "feed DOWN" from "market quiet".
        // NinjaTrader auto-reconnects the data feed itself; this exposes that
        // state and re-arms the writer when the connection is restored.
        private DispatcherTimer heartbeatTimer;
        private volatile bool   connectionUp;   // last known data-feed status
        private DateTime        lastBarWriteUtc; // wall-clock of last bar appended

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description         = @"Historical bar data feed with EMAs and Stochastic D for the Claude AI agent.";
                Name                = "SecondHistoricalData";
                Calculate           = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                BarsRequiredToTrade = 150;
                IsOverlay           = true;

                HistoricalDataFilePath = @"C:\Users\Ad\Documents\Claude-Trader-NinjaTrader\data\HistoricalData.csv";

                // Stochastic defaults — confirm these match what the AI model expects.
                // NT8 Stochastics arg order: (periodD, periodK, smooth)
                StochPeriodD = 14;
                StochPeriodK = 3;
                StochSmooth  = 3;

                // Heartbeat cadence. 15s is well under the Python 90-min stale
                // guard but frequent enough to flag a drop within seconds.
                HeartbeatSeconds = 15;
            }
            else if (State == State.Configure)
            {
                // Assume connected until told otherwise; corrected on first
                // Connection status callback.
                connectionUp    = true;
                lastBarWriteUtc = DateTime.UtcNow;
            }
            else if (State == State.DataLoaded)
            {
                filePath   = HistoricalDataFilePath;
                statusPath = Path.Combine(
                    Path.GetDirectoryName(filePath) ?? ".", "FeedStatus.csv");

                ema21      = EMA(21);
                ema75      = EMA(75);
                ema150     = EMA(150);
                stochastic = Stochastics(StochPeriodD, StochPeriodK, StochSmooth);

                // Truncate + rewrite the header on every load so the file holds ONE
                // clean copy of the loaded window. The previous "write header only if
                // absent" logic preserved old content across reloads, but because
                // OnBarUpdate replays every historical bar on each chart reload it
                // re-appended the whole backfill each time -> duplicate timestamps that
                // break the Python 3-bar FVG-detection window and suppress real gaps.
                // Fresh file per load = no duplicate accumulation, no unbounded growth.
                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(filePath));
                    using (StreamWriter w = new StreamWriter(filePath, false))
                        w.WriteLine("DateTime,Open,High,Low,Close,EMA21,EMA75,EMA150,StochD");
                }
                catch (Exception ex)
                {
                    Print($"SecondHistoricalData init error: {ex.Message}");
                }

                WriteStatus("INIT");
            }
            else if (State == State.Realtime)
            {
                // Start the heartbeat only once we're live; historical replay
                // doesn't need it and runs off the UI thread.
                StartHeartbeat();
            }
            else if (State == State.Terminated)
            {
                StopHeartbeat();
                WriteStatus("TERMINATED");
            }
        }

        // Fired by NinjaTrader whenever the data-feed connection status changes,
        // including its own automatic reconnect attempts.
        protected override void OnConnectionStatusUpdate(ConnectionStatusEventArgs e)
        {
            // We only care about the price/data feed, not order routing.
            bool nowUp = e.PriceStatus == ConnectionStatus.Connected;

            if (nowUp != connectionUp)
            {
                connectionUp = nowUp;
                if (nowUp)
                {
                    Print("SecondHistoricalData: data feed RECONNECTED.");
                    WriteStatus("RECONNECTED");
                }
                else
                {
                    Print("SecondHistoricalData: data feed DISCONNECTED.");
                    WriteStatus("DISCONNECTED");
                }
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < BarsRequiredToTrade)
                return;

            if (ema21 == null || ema75 == null || ema150 == null || stochastic == null)
                return;

            try
            {
                using (StreamWriter w = new StreamWriter(filePath, true))
                {
                    w.WriteLine(
                        $"{Time[0]:yyyy-MM-dd HH:mm:ss},{Open[0]:F2},{High[0]:F2},{Low[0]:F2},{Close[0]:F2}," +
                        $"{ema21[0]:F2},{ema75[0]:F2},{ema150[0]:F2},{stochastic.D[0]:F2}");
                }
                lastBarWriteUtc = DateTime.UtcNow;
            }
            catch (Exception ex)
            {
                Print($"SecondHistoricalData write error: {ex.Message}");
            }
        }

        // --- Heartbeat helpers -------------------------------------------------

        private void StartHeartbeat()
        {
            if (heartbeatTimer != null)
                return;

            // DispatcherTimer runs on the UI thread NinjaScript already uses for
            // chart callbacks, so file writes here don't race OnBarUpdate.
            heartbeatTimer = new DispatcherTimer
            {
                Interval = TimeSpan.FromSeconds(Math.Max(1, HeartbeatSeconds))
            };
            heartbeatTimer.Tick += (s, a) => WriteStatus(connectionUp ? "UP" : "DOWN");
            heartbeatTimer.Start();
        }

        private void StopHeartbeat()
        {
            if (heartbeatTimer == null)
                return;
            heartbeatTimer.Stop();
            heartbeatTimer = null;
        }

        // Single-row status file Python polls. Columns:
        //   Heartbeat = strategy wall-clock now (proves the writer is alive)
        //   Connected = 1/0 data-feed status
        //   State     = INIT|UP|DOWN|RECONNECTED|DISCONNECTED|TERMINATED
        //   LastBar   = wall-clock of the last bar actually appended
        private void WriteStatus(string state)
        {
            if (string.IsNullOrEmpty(statusPath))
                return;
            try
            {
                using (StreamWriter w = new StreamWriter(statusPath, false))
                {
                    w.WriteLine("Heartbeat,Connected,State,LastBar");
                    w.WriteLine(
                        $"{DateTime.Now:yyyy-MM-dd HH:mm:ss}," +
                        $"{(connectionUp ? 1 : 0)},{state}," +
                        $"{lastBarWriteUtc.ToLocalTime():yyyy-MM-dd HH:mm:ss}");
                }
            }
            catch (Exception ex)
            {
                Print($"SecondHistoricalData status error: {ex.Message}");
            }
        }

        #region Properties

        [NinjaScriptProperty]
        [Display(Name = "Historical Data File Path", GroupName = "SecondHistoricalData Parameters", Order = 1,
                 Description = "File that the Claude AI agent reads for bar history and indicators.")]
        public string HistoricalDataFilePath { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "Stoch Period D", GroupName = "SecondHistoricalData Parameters", Order = 2,
                 Description = "NT8 Stochastics first arg (periodD). Standard slow stoch = 14.")]
        public int StochPeriodD { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "Stoch Period K", GroupName = "SecondHistoricalData Parameters", Order = 3,
                 Description = "NT8 Stochastics second arg (periodK). Standard slow stoch = 3.")]
        public int StochPeriodK { get; set; }

        [NinjaScriptProperty]
        [Range(1, 20)]
        [Display(Name = "Stoch Smooth", GroupName = "SecondHistoricalData Parameters", Order = 4,
                 Description = "NT8 Stochastics third arg (smooth). Standard slow stoch = 3.")]
        public int StochSmooth { get; set; }

        [NinjaScriptProperty]
        [Range(1, 300)]
        [Display(Name = "Heartbeat Seconds", GroupName = "SecondHistoricalData Parameters", Order = 5,
                 Description = "How often to write FeedStatus.csv heartbeat so Python can detect a dropped feed.")]
        public int HeartbeatSeconds { get; set; }

        #endregion
    }
}
