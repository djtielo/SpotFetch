import re
import html
import requests
import json

def get_spotify_queries(url):
    """
    Takes a Spotify URL and returns (queries, folder_name).
    folder_name is the playlist/album name, or None for single tracks.
    queries is a list of YouTube search queries: "Track Name Artist Name"
    """
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        r.encoding = 'utf-8'
    except Exception as e:
        print(f"Error fetching Spotify URL: {e}")
        return [], None

    text = r.text
    queries = []
    folder_name = None

    if '/track/' in url:
        m = re.search(r'<title>(.*?)</title>', text)
        if m:
            title = html.unescape(m.group(1)).split(' - song')[0].split(' | Spotify')[0]
            desc_m = re.search(r'<meta property="og:description" content="(.*?)"', text)
            if desc_m:
                desc = html.unescape(desc_m.group(1))
                artist = desc.split('Spotify. ')[-1].split(' \xb7')[0]
                queries.append(f"{title} {artist}")
            else:
                queries.append(title)
    
    elif '/playlist/' in url or '/album/' in url:
        embed_url = url.replace("open.spotify.com", "open.spotify.com/embed")
        try:
            r_embed = requests.get(embed_url, headers=headers, timeout=10)
            r_embed.encoding = 'utf-8'
            text_embed = r_embed.text
        except:
            text_embed = ""
        
        matches = re.finditer(r'"title":"([^"]+)".*?"subtitle":"([^"]+)"', text_embed)
        added = set()
        for i, m in enumerate(matches):
            if i == 0:
                folder_name = html.unescape(m.group(1)).replace('\\"', '"')
                continue
            track = html.unescape(m.group(1)).replace('\\"', '"')
            artist = html.unescape(m.group(2)).replace('\\"', '"')
            query = f"{track} {artist}"
            if query not in added:
                queries.append(query)
                added.add(query)

    return queries, folder_name

if __name__ == '__main__':
    # Test track
    queries, folder = get_spotify_queries("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
    print("Track:", queries, "| folder:", folder)
    # Test playlist
    queries, folder = get_spotify_queries("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
    print("Playlist:", len(queries), "tracks | folder:", folder)
