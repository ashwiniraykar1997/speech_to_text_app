from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
from pydub import AudioSegment
import io, time, json
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, cast
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import DataError
from sqlalchemy import String as SA_String
from supabase_client import (
    insert_transcript,
    get_all_transcripts,
    get_transcripts_for_user,
    get_supabase_client,
    get_user_from_bearer,
)
import os
from dotenv import load_dotenv
import speech_recognition as sr
import requests
from flask import Flask, request, jsonify
from pydub import AudioSegment
from deepgram import Deepgram
import io, os
import asyncio
print("hi in progress")

load_dotenv()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")  # or hardcode for testing
if not DEEPGRAM_API_KEY:
    raise ValueError("DEEPGRAM_API_KEY is not set. Please check your environment variables.")

dg_client = Deepgram(DEEPGRAM_API_KEY)
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true"


# -------------------- Flask setup --------------------
app = Flask(__name__)
CORS(app)

# -------------------- Load environment --------------------
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///transcripts.db")


# -------------------- Database setup --------------------
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Transcript(Base):
    __tablename__ = "transcripts"
    id = Column(Integer, primary_key=True, index=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    duration_seconds = Column(Float, nullable=True)
    filename = Column(String, nullable=True)
    user_id = Column(String, nullable=True)
    language = Column(String, default="en")


Base.metadata.create_all(bind=engine)

# -------------------- Global progress trackers --------------------
live_progress = {
    "progress": 0,
    "text": "",
    "status": "idle",
    "transcript_id": None,
    "total_chunks": 0,
    "processed_chunks": 0,
    "filename": None,
}
file_progress = {"progress": 0, "text": "", "status": "idle", "transcript_id": None}


# -------------------- Routes --------------------
@app.route("/")
def home():
    return jsonify({"message": "✅ Flask backend running successfully!"})

live_progress = {}

# -------------------- Live recording upload --------------------
@app.route("/upload-live", methods=["POST"])
def upload_live():
    global live_progress
    print("Recording started...")
    new_recording = request.form.get("new_recording", "true").lower() == "true"
    if new_recording:
        live_progress = {
            "progress": 0,
            "text": "",
            "status": "processing",
            "transcript_id": None,
            "total_chunks": 0,
            "processed_chunks": 0,
            "filename": None,
        }

    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files["audio"]

    try:
        audio_bytes = io.BytesIO(audio_file.read())
        audio_segment = AudioSegment.from_file(audio_bytes)
        audio_segment = audio_segment.set_frame_rate(16000).set_channels(1)

        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        wav_filename = f"live_recording_{timestamp}.wav"
        audio_segment.export(wav_filename, format="wav")
        live_progress["filename"] = wav_filename

        wav_io = io.BytesIO()
        audio_segment.export(wav_io, format="wav")
        wav_io.seek(0)

        # 🔥 Deepgram transcription
        response = asyncio.run(
            dg_client.transcription.prerecorded(
                {
                    "buffer": wav_io,
                    "mimetype": "audio/wav"  # ✅ move mimetype inside the source dict
                },
                {
                    "punctuate": True,
                    "language": "en",
                    "model": "general",
                }
            )
        )

        text_chunk = response["results"]["channels"][0]["alternatives"][0]["transcript"]

        # Simulate DB record creation
        if not live_progress["transcript_id"]:
            transcript_id = f"transcript_{timestamp}"
            live_progress["transcript_id"] = transcript_id
            live_progress["text"] = text_chunk
        else:
            live_progress["text"] += " " + text_chunk

        live_progress["total_chunks"] += 1
        live_progress["processed_chunks"] += 1
        progress_percent = int(
            (live_progress["processed_chunks"] / live_progress["total_chunks"]) * 100
        )
        live_progress["progress"] = min(progress_percent, 100)
        live_progress["status"] = "processing"

        # Attempt to persist transcript to Supabase with user info if provided
        try:
            def _extract_bearer(req):
                ah = req.headers.get("Authorization") or ""
                if ah.lower().startswith("bearer "):
                    return ah.split(None, 1)[1]
                return None

            token = _extract_bearer(request)
            user = None
            try:
                user = get_user_from_bearer(token) if token else None
            except Exception:
                # ensure resolution errors don't crash the main flow
                print("Warning: user resolution failed during upload_live; continuing without user_id")
                user = None

            if user:
                print(f"upload_live: resolved user id={user.get('id')}")

            # Build record without assuming user exists
            record = {
                'text': text_chunk,
                'filename': wav_filename,
                'duration_seconds': None,
                'created_at': datetime.utcnow().isoformat()
            }
            if user and user.get('id'):
                record['user_id'] = user.get('id')

            # Try to insert to Supabase; if that returns an error or is unavailable,
            # persist to local DB as a fallback so the entry is visible in history.
            resp = None
            try:
                resp = insert_transcript(record)
            except Exception:
                resp = None

            if not resp or (isinstance(resp, dict) and resp.get('error')):
                session = SessionLocal()
                try:
                    t = Transcript(
                        text=text_chunk,
                        filename=wav_filename,
                        duration_seconds=None,
                        created_at=datetime.utcnow(),
                        user_id=(user.get('id') if user else None)
                    )
                    session.add(t)
                    try:
                        session.commit()
                    except DataError:
                        # likely type mismatch (user_id column is integer). Retry without user_id.
                        session.rollback()
                        t.user_id = None
                        try:
                            session.add(t)
                            session.commit()
                        except Exception:
                            session.rollback()
                            print("Failed fallback local insert for live upload even after removing user_id")
                except Exception:
                    print("Failed fallback local insert for live upload")
                finally:
                    session.close()
        except Exception:
            # ignore persistence errors here; they'll be handled by stop-live fallback
            pass

        return jsonify({
            "status": "processed",
            "text": text_chunk,
            "progress": live_progress["progress"],
            "filename": wav_filename
        })

    except Exception as e:
        print("Exception - ", e)
        return jsonify({"error": f"Audio processing failed: {str(e)}"}), 500


# -------------------- Stop live recording --------------------
@app.route("/stop-live", methods=["POST"])
def stop_live():
    global live_progress
    live_progress["status"] = "completed"
    live_progress["progress"] = 100

    # Save final text to DB
    # Try to save to Supabase first if available
    try:
        supabase = get_supabase_client()
        if supabase:
            insert_transcript({
                'text': live_progress["text"],
                'filename': live_progress.get('filename'),
                'duration_seconds': None,
                'created_at': datetime.utcnow().isoformat()
            })
    except Exception:
        # Fallback to local DB (try to include user_id if provided via Authorization)
        try:
            auth_header = request.headers.get("Authorization") or ""
            token = None
            if auth_header.lower().startswith("bearer "):
                token = auth_header.split(None, 1)[1]
            user = get_user_from_bearer(token) if token else None
            user_id = user.get('id') if user else None

            session = SessionLocal()
            # If there is a transcript row from the same live session, update it
            transcript_id = live_progress.get("transcript_id")
            if transcript_id:
                transcript = session.query(Transcript).filter(Transcript.id == transcript_id).first()
                if transcript:
                    transcript.text = live_progress["text"]
                    if user_id:
                        transcript.user_id = user_id
                    session.commit()
            else:
                # Insert a new transcript record locally
                t = Transcript(
                    text=live_progress["text"],
                    filename=live_progress.get('filename'),
                    duration_seconds=None,
                    created_at=datetime.utcnow(),
                    user_id=user_id
                )
                session.add(t)
                session.commit()
            session.close()
        except Exception:
            # best-effort fallback; ignore failures here
            pass

    return jsonify({
        "status": "recording stopped",
        "progress": live_progress["progress"],
        "text": live_progress["text"],
        "filename": live_progress.get("filename")
    })


# -------------------- Download specific live recording --------------------
@app.route("/download-live", methods=["GET"])
def download_live():
    filename = request.args.get("filename")
    if not filename:
        return jsonify({"error": "Filename is required"}), 400
    try:
        return send_file(filename, as_attachment=True)
    except Exception as e:
        return jsonify({"error": f"Download failed: {str(e)}"}), 500


# -------------------- File upload --------------------
@app.route("/upload-file", methods=["POST"])
def upload_file():
    """
    API endpoint: /transcribe
    Accepts an audio file and returns transcribed text using Deepgram.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file part in request."}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file."}), 400

    try:
        # Send file to Deepgram API
        headers = {
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": file.content_type or "audio/wav"
        }

        # Attempt to extract user from Authorization header and attach to record
        def _extract_bearer(req):
            ah = req.headers.get("Authorization") or ""
            if ah.lower().startswith("bearer "):
                return ah.split(None, 1)[1]
            return None

        token = _extract_bearer(request)
        user_id = None
        try:
            user = get_user_from_bearer(token) if token else None
            if user and user.get('id'):
                user_id = user.get('id')
                print(f"upload_file: resolved user id={user_id}")
        except Exception:
            print("Warning: user resolution failed during upload_file; continuing without user_id")
            user_id = None

        response = requests.post(DEEPGRAM_URL, headers=headers, data=file)
        response.raise_for_status()

        result = response.json()
        transcript = result.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])[0].get("transcript", "")

        # Persist transcript with user if available
        try:
            record = {
                'text': transcript,
                'filename': file.filename,
                'duration_seconds': None,
                'created_at': datetime.utcnow().isoformat()
            }
            if user_id:
                record['user_id'] = user_id

            resp = None
            try:
                resp = insert_transcript(record)
            except Exception:
                resp = None

            # If Supabase insert failed or returned an error, write to local DB
            if not resp or (isinstance(resp, dict) and resp.get('error')):
                session = SessionLocal()
                try:
                    t = Transcript(
                        text=transcript,
                        filename=file.filename,
                        duration_seconds=None,
                        created_at=datetime.utcnow(),
                        user_id=user_id
                    )
                    session.add(t)
                    try:
                        session.commit()
                    except DataError:
                        # user_id likely can't accept string; retry without it
                        session.rollback()
                        t.user_id = None
                        try:
                            session.add(t)
                            session.commit()
                        except Exception:
                            session.rollback()
                            print("Failed fallback local insert for file upload after removing user_id")
                finally:
                    session.close()
        except Exception:
            # If any unexpected error happens, try to persist locally as a last resort
            try:
                session = SessionLocal()
                t = Transcript(
                    text=transcript,
                    filename=file.filename,
                    duration_seconds=None,
                    created_at=datetime.utcnow(),
                    user_id=user_id
                )
                session.add(t)
                session.commit()
            except Exception:
                print("Unexpected failure persisting transcript")

        return jsonify({
            "transcript": transcript,
            "deepgram_response": result  # Optional: include full response for debugging
        })

    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

# -------------------- Live progress SSE --------------------
@app.route("/live-stream", methods=["GET"])
def live_stream():
    def generate():
        last_text = ""
        while True:
            if live_progress["text"] != last_text:
                yield f"data: {json.dumps(live_progress)}\n\n"
                last_text = live_progress["text"]
            if live_progress.get("status") == "completed":
                yield f"data: {json.dumps(live_progress)}\n\n"
                break
            time.sleep(0.3)
    return Response(generate(), mimetype="text/event-stream")


# -------------------- Get transcripts --------------------
@app.route("/transcripts", methods=["GET"])
def get_transcripts():
    # If a user_id query param is provided, prefer it.
    user_id_param = request.args.get("user_id")

    # Try to fetch from Supabase, preferring per-user results when possible.
    try:
        supabase = get_supabase_client()
        if supabase:
            # If explicit user_id param is present, use it
            if user_id_param:
                rows = get_transcripts_for_user(user_id_param)
                return jsonify(rows)

            # Otherwise try to resolve user from Authorization header
            def _extract_bearer(req):
                ah = req.headers.get("Authorization") or ""
                if ah.lower().startswith("bearer "):
                    return ah.split(None, 1)[1]
                return None

            token = _extract_bearer(request)
            user = None
            try:
                user = get_user_from_bearer(token) if token else None
            except Exception:
                print("Warning: user resolution failed during /transcripts; returning fallback results")
                user = None

            if user and user.get("id"):
                print(f"/transcripts: resolved user id={user.get('id')}")
                rows = get_transcripts_for_user(user.get("id"))
                return jsonify(rows)

            # Fallback to all transcripts if no user context
            rows = get_all_transcripts()
            return jsonify(rows)
    except Exception:
        pass

    # If Supabase isn't configured or failed, fall back to local DB and support per-user filtering
    session = SessionLocal()
    try:
        if user_id_param:
            try:
                transcripts = session.query(Transcript).filter(Transcript.user_id == user_id_param).order_by(Transcript.created_at.desc()).all()
            except DataError:
                # user_id column may be numeric; cast it to text for comparison
                transcripts = session.query(Transcript).filter(cast(Transcript.user_id, SA_String) == user_id_param).order_by(Transcript.created_at.desc()).all()
        else:
            transcripts = session.query(Transcript).order_by(Transcript.created_at.desc()).all()
    finally:
        session.close()
    # Print a concise summary for debugging instead of raw ORM objects
    try:
        summaries = []
        for t in transcripts:
            try:
                text_preview = (t.text or '').replace('\n', ' ')[:40]
            except Exception:
                text_preview = ''
            summaries.append(
                f"id={t.id} user_id={t.user_id} created_at={t.created_at.isoformat()} text={text_preview}..."
            )
        # print("transcript table summaries:", summaries)
    except Exception:
        # fallback to default repr if something goes wrong
        print("trancript table is", transcripts)
    return jsonify([
        {
            "id": t.id,
            "text": t.text,
            "user_id":t.user_id,
            "created_at": t.created_at.isoformat(),
            "duration_seconds": t.duration_seconds,
            "filename": t.filename,
            "language": t.language,
        }
        for t in transcripts
    ])


# -------------------- Run Flask --------------------
if __name__ == "__main__":
    app.run(debug=True, port=5000)


