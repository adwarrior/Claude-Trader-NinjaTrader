#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.IO;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class SecondLiveFeed : Strategy
    {
        private string filePath;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description         = @"Live price feed - writes current price to a single-row file.";
                Name                = "SecondLiveFeed";
                Calculate           = Calculate.OnEachTick;
                EntriesPerDirection = 1;
                BarsRequiredToTrade = 20;

                LiveFeedFilePath = @"C:\Users\Ad\Documents\Claude-Trader-NinjaTrader\data\LiveFeed.csv";
            }
            else if (State == State.DataLoaded)
            {
                filePath = LiveFeedFilePath;

                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(filePath));
                    using (StreamWriter w = new StreamWriter(filePath, false))
                        w.WriteLine("DateTime,Last");
                }
                catch (Exception ex)
                {
                    Print($"SecondLiveFeed init error: {ex.Message}");
                }
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < BarsRequiredToTrade)
                return;

            // Overwrite with a single current-price row — Python only needs the latest tick.
            try
            {
                using (StreamWriter w = new StreamWriter(filePath, false))
                {
                    w.WriteLine("DateTime,Last");
                    // Use Time[0] (bar/tick time) not DateTime.Now — correct in replay/sim.
                    w.WriteLine($"{Time[0]:yyyy-MM-dd HH:mm:ss},{Close[0]:F2}");
                }
            }
            catch (Exception ex)
            {
                Print($"SecondLiveFeed write error: {ex.Message}");
            }
        }

        #region Properties

        [NinjaScriptProperty]
        [Display(Name = "Live Feed File Path", GroupName = "SecondLiveFeed Parameters", Order = 1,
                 Description = "File that Python reads for the current market price.")]
        public string LiveFeedFilePath { get; set; }

        #endregion
    }
}
