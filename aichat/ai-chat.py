import os
from typing import List, Dict, Any, Set
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
import google.generativeai as genai
from difflib import SequenceMatcher
from pydantic import BaseModel
import random


# Load .env file
load_dotenv()

# Initialize FastAPI
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load API keys from .env
api_keys = os.getenv("API_KEYS", "").split(",")
if not api_keys or api_keys == [""]:
    raise RuntimeError("No API keys found in the .env file.")

# Function to get a random API key
def get_random_api_key():
    return random.choice(api_keys)

# Set up the generative AI model with a random API key
genai.configure(api_key=get_random_api_key())
model = genai.GenerativeModel("gemini-1.5-flash")
class RecommendationRequest(BaseModel):
    query: str
    excluded_titles: List[str] = []
    is_follow_up: bool = False  # Tambah field untuk track follow-up


def get_anime_list():
    url = "https://ccgnimex.my.id/v2/android/api_browse.php"
    try:
        response = requests.get(url).json()
        for anime in response:
            anime['anime_id'] = anime.get('anime_id', None)
        return response
    except Exception:
        raise HTTPException(status_code=500, detail="Gagal mengambil data anime")

def fuzzy_match(gemini_title: str, api_titles: List[str]) -> str:
    best_match = None
    best_ratio = 0.55
    
    for title in api_titles:
        ratio = SequenceMatcher(None, gemini_title.lower(), title.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = title
            
    return best_match or gemini_title

def get_gemini_recommendations(user_query: str, excluded_titles: List[str], is_follow_up: bool):
    base_query = user_query.replace("Cari lagi selain itu", "").strip() if is_follow_up else user_query
    
    prompt = f"""
    Berikan rekomendasi anime berdasarkan: "{base_query}"
    {f"- Jangan sertakan: {', '.join(excluded_titles)}" if excluded_titles else ""}
    
    Format:
    1. Judul 1
    2. Judul 2
    ...
    """
    
    try:
        response = model.generate_content(prompt)
        return [line.split('. ')[1] for line in response.text.split('\n') 
                if line.strip() and line[0].isdigit()]
    except Exception:
        raise HTTPException(status_code=500, detail="Error memproses permintaan")

def validate_recommendations(gemini_titles: List[str], api_data: List[Dict]) -> List[Dict]:
    validated = []
    api_titles = [a['judul'] for a in api_data]
    
    for title in gemini_titles:
        matched = fuzzy_match(title, api_titles)
        validated.extend([a for a in api_data if a['judul'] == matched])
    
    seen = set()
    return [x for x in validated if not (x['judul'] in seen or seen.add(x['judul']))]

def get_followup_recommendations(api_data: List[Dict], excluded_titles: List[str]) -> List[Dict]:
    attributes_set: Set[str] = set()
    excluded_juduls = set(excluded_titles)
    
    # Kumpulkan atribut dari yang di-exclude
    for anime in api_data:
        if anime['judul'] in excluded_juduls:
            studios = [s.strip().lower() for s in anime.get('studios', '').split(',') if s.strip()]
            season = anime.get('season', '').strip().lower()
            tags = [t.strip().lower() for t in anime.get('tags', '').split(',') if t.strip()]
            genre = [g.strip().lower() for g in anime.get('genre', '').split(',') if g.strip()]
            
            attributes_set.update(studios)
            if season: attributes_set.add(season)
            attributes_set.update(tags)
            attributes_set.update(genre)
    
    # Hitung skor similarity dengan shuffle
    similarity_scores = []
    for anime in api_data:
        if anime['judul'] in excluded_juduls:
            continue
        
        their_studios = [s.strip().lower() for s in anime.get('studios', '').split(',') if s.strip()]
        their_season = anime.get('season', '').strip().lower()
        their_tags = [t.strip().lower() for t in anime.get('tags', '').split(',') if t.strip()]
        their_genre = [g.strip().lower() for g in anime.get('genre', '').split(',') if g.strip()]
        
        their_attributes = set(their_studios + [their_season] + their_tags + their_genre)
        overlap = len(their_attributes.intersection(attributes_set))
        
        similarity_scores.append((overlap, anime))
    
    # Kelompokkan berdasarkan skor similarity
    score_groups = {}
    for score, anime in similarity_scores:
        if score not in score_groups:
            score_groups[score] = []
        score_groups[score].append(anime)
    
    # Acak urutan dalam kelompok skor yang sama
    for group in score_groups.values():
        random.shuffle(group)
    
    # Urutkan kelompok dari skor tertinggi
    sorted_groups = sorted(score_groups.items(), key=lambda x: -x[0])
    
    # Gabungkan hasil dengan prioritas skor tinggi
    final_recommendations = []
    for score, animes in sorted_groups:
        final_recommendations.extend(animes)
    
    return final_recommendations[:500]  # Ambil lebih banyak untuk diacak

@app.post("/recommend")
async def get_recommendations(request: RecommendationRequest):
    try:
        api_data = get_anime_list()
        if not api_data:
            raise HTTPException(status_code=404, detail="Data anime tidak ditemukan")

        if request.is_follow_up:
            # Ambil lebih banyak hasil lalu acak
            all_recommendations = get_followup_recommendations(api_data, request.excluded_titles)
            
            # Filter yang belum pernah direkomendasikan
            new_recommendations = [a for a in all_recommendations 
                                 if a['judul'] not in request.excluded_titles]
            
            # Acak urutan dan ambil 6 unik
            random.shuffle(new_recommendations)
            filtered = list({a['judul']: a for a in new_recommendations}.values())[:6]
            
            ai_message = f"Menemukan {len(filtered)} rekomendasi baru yang mungkin tertarik:"
        else:
            original_query = request.query
            gemini_titles = get_gemini_recommendations(
                original_query,
                request.excluded_titles,
                request.is_follow_up
            )
            validated = validate_recommendations(gemini_titles, api_data)
            filtered = [a for a in validated 
                      if a['judul'] not in request.excluded_titles]
            ai_message = f"Menemukan {len(filtered)} rekomendasi untuk '{original_query}':"
        
        return {
            "ai": ai_message,
            "results": filtered
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
