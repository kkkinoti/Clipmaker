import streamlit as st
import soccerdata as sd
import pandas as pd
import os

st.set_page_config(page_title="Pro Football Scraper", page_icon="⚽")

st.title("⚽ Multipurpose Match Scraper")
st.markdown("Enter the details below to generate a ClipMaker-ready CSV.")

# --- SIDEBAR INPUTS ---
with st.sidebar:
    st.header("Settings")
    season = st.selectbox("Season", ["2526", "2425"], index=0)
    league = st.text_input("League", "ENG-Premier League")
    # Add a toggle for headless mode (keep False to see the browser if it hangs)
    headless = st.checkbox("Run in Background (Headless)", value=False)

# --- MAIN INTERFACE ---
col1, col2 = st.columns(2)
with col1:
    match_id = st.text_input("Match ID", placeholder="e.g., 1903304")
with col2:
    player_name = st.text_input("Player Name", placeholder="e.g., Bruno Fernandes")

if st.button("🚀 Fetch Data"):
    if not match_id or not player_name:
        st.error("Please provide both a Match ID and a Player Name.")
    else:
        with st.status("Connecting to WhoScored...", expanded=True) as status:
            try:
                # Initialize Scraper
                ws = sd.WhoScored(leagues=league, seasons=season, no_cache=True, headless=headless)
                
                status.write("📥 Downloading events...")
                events = ws.read_events(match_id=int(match_id))
                
                status.write(f"🔍 Filtering for {player_name}...")
                # Case-insensitive filter
                mask = events['player'].str.contains(player_name, case=False, na=False)
                df = events[mask].copy()

                if df.empty:
                    st.warning(f"No events found for '{player_name}'. Check spelling or Match ID.")
                    st.write("Available players in this match:", events['player'].unique().tolist())
                else:
                    # ClipMaker Formatting
                    if 'minute' not in df.columns and 'timestamp' in df.columns:
                        df[['minute', 'second']] = df['timestamp'].str.split(':', expand=True).astype(int)
                    
                    df['event_type'] = df['type']
                    final_csv = df[['period', 'minute', 'second', 'type', 'event_type', 'player']]
                    
                    # Save local copy
                    filename = f"{player_name.replace(' ', '_')}_match_{match_id}.csv"
                    final_csv.to_csv(filename, index=False)
                    
                    status.update(label="✅ Data Ready!", state="complete")
                    st.success(f"Successfully captured {len(df)} events!")
                    
                    # Download button for convenience
                    st.download_button(
                        label="💾 Download CSV for ClipMaker",
                        data=final_csv.to_csv(index=False),
                        file_name=filename,
                        mime='text/csv'
                    )
            except Exception as e:
                st.error(f"Error: {e}")
