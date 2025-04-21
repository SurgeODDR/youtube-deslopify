#!/usr/bin/env python3

import os
# Removed BeautifulSoup import as it's no longer needed
# from bs4 import BeautifulSoup 
import json
from datetime import datetime
from collections import Counter

# def parse_watch_history(html_file):
def parse_watch_history_json(json_file):
    """
    # Parse the YouTube watch history HTML file and extract relevant information.
    Parse the YouTube watch history JSON file and extract relevant information.
    Returns a list of dictionaries containing video information.
    """
    # with open(html_file, 'r', encoding='utf-8') as f:
    #     soup = BeautifulSoup(f.read(), 'html.parser')
    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
        return []
    except FileNotFoundError:
        print(f"Error: Input JSON file not found at {json_file}")
        return []

    # Find all video entries
    # entries = soup.find_all('div', class_='content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1')
    
    watch_history = []
    
    # for entry in entries:
    for entry in data:
        video_data = {}
        
        # Extract video title and URL
        # title_link = entry.find('a')
        # if title_link:
        #     video_data['title'] = title_link.text.strip()
        #     video_data['url'] = title_link.get('href', '')
        if entry.get('title') and entry.get('titleUrl'):
            # Remove "Watched " prefix if present
            video_data['title'] = entry['title'].replace('Watched ', '', 1).strip()
            video_data['url'] = entry['titleUrl']
            
            # Extract video ID from URL if possible
            if 'watch?v=' in video_data['url']:
                video_data['video_id'] = video_data['url'].split('watch?v=')[1].split('&')[0]
        
        # Extract channel information - updated to find all links and identify channel link
        # links = entry.find_all('a')
        # for link in links:
        #     href = link.get('href', '')
        #     text = link.text.strip()
        #     # Skip empty text or single character channel names (likely parsing errors)
        #     if not text or len(text) <= 1:
        #         continue
        #     # Skip if the link text is the same as the video title (to avoid duplicate entries)
        #     if 'title' in video_data and text == video_data['title']:
        #         continue
        #     if '/channel/' in href or '/c/' in href or '/user/' in href:
        #         video_data['channel'] = text
        #         video_data['channel_url'] = href
        #         if '/channel/' in href:
        #             video_data['channel_id'] = href.split('/channel/')[1]
        #         break  # Stop after finding the first valid channel link
        if entry.get('subtitles'):
            subtitles = entry['subtitles']
            if isinstance(subtitles, list) and len(subtitles) > 0:
                channel_info = subtitles[0]
                if channel_info.get('name') and channel_info.get('url'):
                    video_data['channel'] = channel_info['name']
                    video_data['channel_url'] = channel_info['url']
                    # Attempt to extract channel ID from URL
                    if '/channel/' in video_data['channel_url']:
                         video_data['channel_id'] = video_data['channel_url'].split('/channel/')[1]
                    elif '/c/' in video_data['channel_url']:
                         # Channel ID might not be directly available for /c/ URLs
                         video_data['channel_id'] = f"custom_url:{video_data['channel_url'].split('/c/')[1]}"
                    elif '/user/' in video_data['channel_url']:
                         # Channel ID might not be directly available for /user/ URLs
                         video_data['channel_id'] = f"user:{video_data['channel_url'].split('/user/')[1]}"

        # Extract timestamp
        # timestamp = entry.find('div', {'class': 'content-cell mdl-cell mdl-cell--12-col mdl-typography--caption'})
        # if timestamp:
        #     video_data['timestamp'] = timestamp.text.strip()
        if entry.get('time'):
             video_data['timestamp'] = entry['time'] # Already in ISO format
            
        # if video_data and 'channel' in video_data:  # Only append if we found both video and channel data
        #     watch_history.append(video_data)
        # Only append if we have title, url, and channel info
        if all(k in video_data for k in ('title', 'url', 'channel', 'channel_url', 'timestamp')):
            watch_history.append(video_data)
        else:
            # Optionally log entries that are missing essential data
            # print(f"Skipping entry due to missing data: {entry}")
            pass
    
    return watch_history

def analyze_channel_statistics(watch_history):
    """
    Analyze channel statistics from watch history data.
    Returns a dictionary containing various channel-related statistics.
    """
    # Create a mapping of channel names to their URLs
    channel_urls = {}
    for entry in watch_history:
        if 'channel' in entry and 'channel_url' in entry:
            channel_urls[entry['channel']] = entry['channel_url']

    # Extract all channels
    channels = [entry['channel'] for entry in watch_history if 'channel' in entry]
    channel_counts = Counter(channels)
    
    # Get total number of unique channels
    unique_channels = len(channel_counts)
    
    # Get top 10 most watched channels with their watch counts
    top_channels = channel_counts.most_common(10)
    
    # Calculate percentage of total views for each top channel
    total_views = len(channels)
    top_channels_with_percentage = [
        {
            'channel': channel,
            'views': count,
            'percentage': round((count / total_views) * 100, 2)
        }
        for channel, count in top_channels
    ]
    
    # Get top 50 channel URLs
    top_50_channels = channel_counts.most_common(50)
    top_50_channel_urls = [
        {
            'channel': channel,
            'url': channel_urls.get(channel, ''),
            'views': count
        }
        for channel, count in top_50_channels
    ]
    
    # Get channels by view count distribution
    view_distribution = {
        '1 view': sum(1 for count in channel_counts.values() if count == 1),
        '2-5 views': sum(1 for count in channel_counts.values() if 2 <= count <= 5),
        '6-10 views': sum(1 for count in channel_counts.values() if 6 <= count <= 10),
        '11-20 views': sum(1 for count in channel_counts.values() if 11 <= count <= 20),
        '21+ views': sum(1 for count in channel_counts.values() if count > 20)
    }
    
    # Add all channels with their view counts
    all_channels = [
        {'channel': channel, 'views': count}
        for channel, count in channel_counts.items()
    ]
    
    return {
        'total_videos': total_views,
        'unique_channels': unique_channels,
        'top_channels': top_channels_with_percentage,
        'top_50_channel_urls': top_50_channel_urls,
        'view_distribution': view_distribution,
        'all_channels': sorted(all_channels, key=lambda x: x['views'], reverse=True)
    }

def save_data(watch_history, output_dir):
    """
    Save the watch history data and statistics as JSON files
    """
    # Save raw watch history
    json_path = os.path.join(output_dir, 'watch_history.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(watch_history, f, indent=2, ensure_ascii=False)
    
    # Generate and save statistics
    stats = analyze_channel_statistics(watch_history)
    stats_path = os.path.join(output_dir, 'channel_statistics.json')
    with open(stats_path, 'w', encoding='utf-8') as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    
    return json_path, stats_path

def main():
    # Get the directory of this script
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Input and output paths
    # input_file = os.path.join(current_dir, 'watch-history.html')
    input_file = os.path.join(current_dir, 'watch-history.json') # Updated input filename
    
    if not os.path.exists(input_file):
        # print(f"Error: Could not find watch history file at {input_file}")
        print(f"Error: Could not find watch history JSON file at {input_file}") # Updated error message
        return
    
    # print("Parsing watch history...")
    print("Parsing watch history JSON...") # Updated print message
    # watch_history = parse_watch_history(input_file)
    watch_history = parse_watch_history_json(input_file) # Updated function call
    
    print(f"Found {len(watch_history)} valid video entries") # Updated print message
    
    # Save the data and generate statistics
    json_path, stats_path = save_data(watch_history, current_dir)
    
    print(f"\nData has been saved to:")
    print(f"Watch History: {json_path}")
    print(f"Channel Statistics: {stats_path}")
    
    # Print some key statistics
    with open(stats_path, 'r', encoding='utf-8') as f:
        stats = json.load(f)
    
    print("\nChannel Statistics Summary:")
    print(f"Total videos watched: {stats['total_videos']}")
    print(f"Unique channels: {stats['unique_channels']}")
    print("\nTop 5 Most Watched Channels:")
    for i, channel in enumerate(stats['top_channels'][:5], 1):
        print(f"{i}. {channel['channel']}: {channel['views']} views ({channel['percentage']}%)")

if __name__ == "__main__":
    main() 