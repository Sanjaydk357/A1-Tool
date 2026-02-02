import os
import io
import uuid
import time
import yt_dlp
import zipfile
import tempfile
from PIL import Image
from docx2pdf import convert
from pydub import AudioSegment
from pdf2docx import Converter
from PyPDF2 import PdfReader, PdfWriter, PdfMerger
from moviepy import VideoFileClip, AudioFileClip, AudioArrayClip, concatenate_videoclips, concatenate_audioclips
from flask import Flask, request, send_file, render_template, flash, redirect, url_for, jsonify, send_from_directory ,session

app = Flask(__name__)
upload_folder = 'upload'
os.makedirs(upload_folder, exist_ok=True)
app.secret_key = 'supersecret'

progress_status = {} 

def my_progress_hook(d):
    """
    Callback function for yt-dlp.
    It formats the string exactly as requested.
    """
    if d['status'] == 'downloading':
        task_id = d['info_dict'].get('task_id')
        if task_id:
            # Clean up raw data (remove color codes if any, handle missing keys)
            percent = d.get('_percent_str', '0%').replace('\x1b[0;94m', '').replace('\x1b[0m', '')
            total = d.get('_total_bytes_str') or d.get('_total_bytes_estimate_str', 'Unknown')
            total = total.replace('\x1b[0;94m', '').replace('\x1b[0m', '')
            speed = d.get('_speed_str', 'N/A').replace('\x1b[0;94m', '').replace('\x1b[0m', '')
            eta = d.get('_eta_str', 'N/A').replace('\x1b[0;94m', '').replace('\x1b[0m', '')

            # Format: [download]  31.8% of ~ 207.90MiB at    2.42MiB/s ETA 01:14
            status_str = f"[download] {percent} of {total} at {speed} ETA {eta}"
            progress_status[task_id] = status_str

    elif d['status'] == 'finished':
        task_id = d['info_dict'].get('task_id')
        if task_id:
            progress_status[task_id] = "Download complete! Processing..."

# --- ADD THIS NEW ROUTE (To fetch progress) ---
@app.route('/progress/<task_id>', methods=['GET'])
def get_progress(task_id):
    return jsonify({"status": progress_status.get(task_id, "Starting...")})


# Helper to redirect to home on error
def home_with_error(message):
    flash(message, "error")
    return redirect(url_for('index'))

@app.route('/')
def index():
    return render_template("index.html")

# --- VIDEO TOOLS ---

@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(upload_folder, filename, as_attachment=True)

@app.route("/merge-videos", methods=["POST"])
def merge_videos():
    try:
        # Get list of files from the form with name="videos[]"
        video_files = request.files.getlist("videos[]")

        if not video_files or len(video_files) < 2:
            return jsonify({"error": "Please upload at least two videos."}), 400

        clips = []
        temp_paths = []

        # 1. Save all uploaded files temporarily and load clips
        for video in video_files:
            # Create a unique temp name to avoid collisions
            temp_filename = f"temp_{uuid.uuid4().hex}.mp4"
            temp_path = os.path.join(upload_folder, temp_filename)
            video.save(temp_path)
            temp_paths.append(temp_path)
            
            # Load clip
            clips.append(VideoFileClip(temp_path))

        # 2. Standardization: Resize all clips to the size of the first clip
        target_w, target_h = clips[0].size
        target_fps = clips[0].fps if clips[0].fps else 24
        
        processed_clips = []
        for clip in clips:
            # Resize and set FPS to match the first video
            new_clip = clip.resized(width=target_w, height=target_h).with_fps(target_fps)
            processed_clips.append(new_clip)

        # 3. Concatenate
        final_clip = concatenate_videoclips(processed_clips)
        
        # 4. Write Output
        output_filename = f"merged_{uuid.uuid4().hex}.mp4"
        output_path = os.path.join(upload_folder, output_filename)
        
        # Use a faster preset for web responsiveness
        final_clip.write_videofile(output_path, codec="libx264", audio_codec="aac", preset="ultrafast")
        
        # 5. Cleanup Resources
        final_clip.close()
        for clip in clips:
            clip.close()
        for path in temp_paths:
            if os.path.exists(path):
                os.remove(path)

        # 6. Return JSON response with the download URL
        return jsonify({
            "success": True, 
            "message": "Videos merged successfully!",
            "download_url": url_for('download_file', filename=output_filename)
        })

    except Exception as e:
        print(e)
        return jsonify({"error": str(e)}), 500

@app.route('/video-to-audio', methods=["POST"])
def video_to_audio():
    video = request.files.get("video")
    if not video:
        return home_with_error("No video file uploaded.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_video:
        temp_video.write(video.read())
        video_path = temp_video.name

    try:
        clip = VideoFileClip(video_path)
        if clip.audio is None:
            clip.close()
            os.remove(video_path)
            return home_with_error("No audio track found in the uploaded video.")

        output_audio_path = os.path.join(upload_folder, "extracted_audio.mp3")
        clip.audio.write_audiofile(output_audio_path)
        clip.close()
        os.remove(video_path)

        return send_file(output_audio_path, as_attachment=True, mimetype='audio/mpeg')
    except Exception as e:
        if os.path.exists(video_path): os.remove(video_path)
        return home_with_error(f"Error: {str(e)}")

@app.route('/screen-recorder', methods=["POST"])
def screen_recorder():
    recorded_file = request.files.get("recording")
    if not recorded_file:
        return home_with_error("No recording received.")

    output_path = os.path.join(upload_folder, "screen_recording.webm")
    recorded_file.save(output_path)
    return send_file(output_path, as_attachment=True)

@app.route('/download-video', methods=["POST"])
def download_video():
    video_url = request.form.get("video_url")
    task_id = request.form.get("task_id") # We will send this from JS

    if not video_url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        temp_dir = upload_folder # Save directly to upload folder to serve later
        output_filename = f"%(title)s_%(id)s.%(ext)s"
        output_path = os.path.join(temp_dir, output_filename)

        ydl_opts = {
            'outtmpl': output_path,
            'format': 'mp4',
            'quiet': True,
            'progress_hooks': [my_progress_hook], # Attach the hook
        }

        # Inject task_id into info_dict so the hook can see it
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # We add task_id to params so it's accessible in the hook via info_dict? 
            # Actually, standard yt-dlp doesn't pass extra params easily to hooks.
            # Workaround: Use a wrapper or set it in a way available to the hook.
            # Simpler approach for this Context:
            ydl.params['info_dict'] = {'task_id': task_id} # Hacky but works for simple context
            
            # Better approach: Define hook inside here (closure)
            def inner_hook(d):
                if d['status'] == 'downloading':
                    p = d.get('_percent_str', '0%')
                    t = d.get('_total_bytes_str') or d.get('_total_bytes_estimate_str', '?')
                    s = d.get('_speed_str', '?')
                    e = d.get('_eta_str', '?')
                    # Strip colors
                    p = p.replace('\x1b[0;94m', '').replace('\x1b[0m', '')
                    progress_status[task_id] = f"[download] {p} of {t} at {s} ETA {e}"
                elif d['status'] == 'finished':
                     progress_status[task_id] = "Processing final file..."

            ydl.add_progress_hook(inner_hook)
            
            info = ydl.extract_info(video_url, download=True)
            final_filename = ydl.prepare_filename(info)
            final_basename = os.path.basename(final_filename)

        # Cleanup progress
        if task_id in progress_status:
            del progress_status[task_id]

        return jsonify({
            "success": True,
            "download_url": url_for('download_file', filename=final_basename)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- AUDIO TOOLS ---

@app.route('/trim-audio', methods=["POST"])
def trim_audio():
    audio_file = request.files.get("audio")
    try:
        start_time = float(request.form.get("start", 0))
        end_time = float(request.form.get("end", 0))
    except ValueError:
        return home_with_error("Invalid start or end time.")

    if not audio_file:
        return home_with_error("No audio file uploaded.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_audio:
        temp_audio.write(audio_file.read())
        temp_audio_path = temp_audio.name

    try:
        audio_clip = AudioFileClip(temp_audio_path)
        if end_time > audio_clip.duration: end_time = audio_clip.duration
        
        trimmed_audio = audio_clip.subclip(start_time, end_time)
        output_path = os.path.join(upload_folder, "trimmed_audio.mp3")
        trimmed_audio.write_audiofile(output_path)
        
        audio_clip.close()
        trimmed_audio.close()
        return send_file(output_path, as_attachment=True)
    except Exception as e:
        return home_with_error(f"Trim Error: {str(e)}")
    finally:
        if os.path.exists(temp_audio_path): os.remove(temp_audio_path)

@app.route('/merge-audio', methods=["POST"])
def merge_audio():
    audio1 = request.files.get("audio1")
    audio2 = request.files.get("audio2")
    if not audio1 or not audio2:
        return home_with_error("Please upload both audio files.")

    # Using simplistic temp file approach
    temp1 = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    temp2 = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    temp1.write(audio1.read())
    temp2.write(audio2.read())
    temp1.close()
    temp2.close()

    try:
        clip1 = AudioFileClip(temp1.name)
        clip2 = AudioFileClip(temp2.name)
        final_audio = concatenate_audioclips([clip1, clip2])
        output_path = os.path.join(upload_folder, "merged_audio.mp3")
        final_audio.write_audiofile(output_path)
        
        clip1.close()
        clip2.close()
        os.remove(temp1.name)
        os.remove(temp2.name)
        return send_file(output_path, as_attachment=True)
    except Exception as e:
        return home_with_error(f"Audio Merge Error: {str(e)}")

@app.route('/record-audio', methods=["POST"])
def record_audio():
    audio_file = request.files.get("audio")
    if not audio_file:
        return home_with_error("No audio recorded.")

    temp_webm_path = os.path.join(upload_folder, "temp_audio.webm")
    audio_file.save(temp_webm_path)
    mp3_path = os.path.join(upload_folder, "recorded_audio.mp3")
    
    try:
        audio = AudioSegment.from_file(temp_webm_path, format="webm")
        audio.export(mp3_path, format="mp3")
        os.remove(temp_webm_path)
        return send_file(mp3_path, as_attachment=True)
    except Exception as e:
        return home_with_error(f"Recording Error: {str(e)}")

@app.route('/reverse-audio', methods=["POST"])
def reverse_audio():
    audio_file = request.files.get("audio")
    if not audio_file:
        return home_with_error("Please upload an audio file.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_audio:
        temp_audio.write(audio_file.read())
        temp_audio_path = temp_audio.name

    try:
        clip = AudioFileClip(temp_audio_path)
        audio_array = clip.to_soundarray()
        reversed_array = audio_array[::-1]
        reversed_clip = AudioArrayClip(reversed_array, fps=clip.fps)

        output_path = os.path.join(upload_folder, "reversed_audio.mp3")
        reversed_clip.write_audiofile(output_path)
        
        clip.close()
        reversed_clip.close()
        os.remove(temp_audio_path)
        return send_file(output_path, as_attachment=True)
    except Exception as e:
        return home_with_error(f"Reverse Error: {str(e)}")

@app.route('/download-audio', methods=["POST"])
def download_audio():
    video_url = request.form.get("video_url")
    task_id = request.form.get("task_id")

    if not video_url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(upload_folder, "%(title)s.%(ext)s"),
            'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}],
            'quiet': True,
        }

        # Closure hook
        def inner_hook(d):
            if d['status'] == 'downloading':
                p = d.get('_percent_str', '0%').replace('\x1b[0;94m', '').replace('\x1b[0m', '')
                t = d.get('_total_bytes_str') or d.get('_total_bytes_estimate_str', '?')
                s = d.get('_speed_str', '?')
                e = d.get('_eta_str', '?')
                progress_status[task_id] = f"[download] {p} of {t} at {s} ETA {e}"
            elif d['status'] == 'finished':
                    progress_status[task_id] = "Converting to MP3..."

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.add_progress_hook(inner_hook)
            info = ydl.extract_info(video_url, download=True)
            final_path = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
            final_basename = os.path.basename(final_path)

        if task_id in progress_status: del progress_status[task_id]

        return jsonify({
            "success": True,
            "download_url": url_for('download_file', filename=final_basename)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- PDF / IMAGE TOOLS ---

@app.route('/split-pages', methods=["POST"])
def split_pages():
    pdf_file = request.files.get("pdf")
    if not pdf_file:
        return home_with_error("No PDF uploaded.")

    try:
        reader = PdfReader(pdf_file)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w') as zipf:
            for i, page in enumerate(reader.pages):
                writer = PdfWriter()
                writer.add_page(page)
                page_io = io.BytesIO()
                writer.write(page_io)
                zipf.writestr(f"page_{i + 1}.pdf", page_io.getvalue())
        zip_buffer.seek(0)
        return send_file(zip_buffer, as_attachment=True, download_name="split_pages.zip", mimetype='application/zip')
    except Exception as e:
        return home_with_error(f"Split Error: {str(e)}")

@app.route('/merge-pdf', methods=["POST"])
def merge_pdf():
    uploaded_files = request.files.getlist("pdfs")
    if not uploaded_files or len(uploaded_files) < 2:
        return home_with_error("Please upload at least two PDF files.")

    try:
        merger = PdfMerger()
        for file in uploaded_files:
            merger.append(file)
        output_path = os.path.join(upload_folder, "merged.pdf")
        with open(output_path, "wb") as f:
            merger.write(f)
        merger.close()
        return send_file(output_path, as_attachment=True)
    except Exception as e:
        return home_with_error(f"Merge PDF Error: {str(e)}")

@app.route('/pdf-to-word', methods=["POST"])
def pdf_to_word():
    pdf_file = request.files.get("pdf")
    if not pdf_file:
        return home_with_error("Please upload a PDF file.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
        temp_pdf.write(pdf_file.read())
        pdf_path = temp_pdf.name
    output_docx = os.path.join(upload_folder, "converted.docx")

    try:
        cv = Converter(pdf_path)
        cv.convert(output_docx)
        cv.close()
        os.remove(pdf_path)
        return send_file(output_docx, as_attachment=True)
    except Exception as e:
        return home_with_error(f"PDF to Word Error: {str(e)}")

@app.route('/word-to-pdf', methods=["POST"])
def word_to_pdf():
    docx_file = request.files.get("word")
    if not docx_file:
        return home_with_error("Please upload a Word file.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as temp_docx:
        temp_docx.write(docx_file.read())
        input_path = temp_docx.name
    output_pdf_path = os.path.join(upload_folder, "converted.pdf")

    try:
        convert(input_path, output_pdf_path)
        os.remove(input_path)
        return send_file(output_pdf_path, as_attachment=True)
    except Exception as e:
        return home_with_error(f"Word to PDF Error: {str(e)}")

@app.route('/image-to-pdf', methods=["POST"])
def image_to_pdf():
    image_files = request.files.getlist("images")
    if not image_files:
        return home_with_error("Please upload images.")

    images = []
    temp_files = []
    try:
        for image_file in image_files:
            temp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            temp.write(image_file.read())
            temp.close()
            temp_files.append(temp.name)
            img = Image.open(temp.name).convert("RGB")
            images.append(img)

        output_pdf_path = os.path.join(upload_folder, "merged_images.pdf")
        images[0].save(output_pdf_path, save_all=True, append_images=images[1:], format="PDF")
        
        for path in temp_files: os.remove(path)
        return send_file(output_pdf_path, as_attachment=True)
    except Exception as e:
        return home_with_error(f"Image to PDF Error: {str(e)}")

@app.route('/compress-zip', methods=["POST"])
def compress_zip():
    files = request.files.getlist("files")
    if not files:
        return home_with_error("Please upload files to compress.")

    zip_filename = os.path.join(upload_folder, "compressed_files.zip")
    try:
        with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file in files:
                file_path = os.path.join(upload_folder, file.filename)
                file.save(file_path)
                zipf.write(file_path, arcname=file.filename)
                os.remove(file_path)
        return send_file(zip_filename, as_attachment=True)
    except Exception as e:
        return home_with_error(f"Compression Error: {str(e)}")

if __name__ == "__main__":
    app.run(debug=True)
