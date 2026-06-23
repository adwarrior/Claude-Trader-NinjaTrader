#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.IO;
using System.Windows.Media;
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
        private EMA ema21;
        private EMA ema75;
        private EMA ema150;
        private Stochastics stochastic;

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

                HistoricalDataFilePath = @"C:\ClaudeTrader\data\HistoricalData.csv";

                // Stochastic defaults — confirm these match what the AI model expects.
                // NT8 Stochastics arg order: (periodD, periodK, smooth)
                StochPeriodD = 14;
                StochPeriodK = 3;
                StochSmooth  = 3;
            }
            else if (State == State.DataLoaded)
            {
                filePath = HistoricalDataFilePath;

                ema21      = EMA(21);
                ema75      = EMA(75);
                ema150     = EMA(150);
                stochastic = Stochastics(StochPeriodD, StochPeriodK, StochSmooth);

                // Write header once — prevents mid-session reload from wiping accumulated history.
                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(filePath));
                    if (!File.Exists(filePath))
                    {
                        using (StreamWriter w = new StreamWriter(filePath, false))
                            w.WriteLine("DateTime,Open,High,Low,Close,EMA21,EMA75,EMA150,StochD");
                    }
                }
                catch (Exception ex)
                {
                    Print($"SecondHistoricalData init error: {ex.Message}");
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
            }
            catch (Exception ex)
            {
                Print($"SecondHistoricalData write error: {ex.Message}");
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

        #endregion
    }
}
