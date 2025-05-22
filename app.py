from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file
import threading
import json
import os
from pathlib import Path
from datetime import datetime
from backend.amazon_review import AmazonReviewExtractor
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
app.config['REVIEWS_DIR'] = 'reviews'

# Ensure reviews directory exists
Path(app.config['REVIEWS_DIR']).mkdir(exist_ok=True)

# Thread-safe extraction status
extraction_status_lock = threading.Lock()
extraction_status = {
    'running': False,
    'progress': 0,
    'message': '',
    'results_file': None,
    'summary_file': None,
    'product_url': None,
    'num_pages': None,
    'error': None
}

def run_extraction(product_url, num_pages, output_format, debug=True):
    global extraction_status
    try:
        with extraction_status_lock:
            extraction_status.update({
                'running': True,
                'progress': 10,
                'message': "Initializing extractor...",
                'results_file': None,
                'summary_file': None,
                'product_url': product_url,
                'num_pages': num_pages,
                'error': None
            })

        extractor = AmazonReviewExtractor()

        with extraction_status_lock:
            extraction_status.update({
                'progress': 30,
                'message': "Setting up WebDriver..."
            })

        with extraction_status_lock:
            extraction_status.update({
                'progress': 50,
                'message': f"Extracting up to {num_pages or 'all'} pages..."
            })

        result = extractor.run(product_url=product_url, max_pages=num_pages, save_format=output_format, debug=debug)
        
        with extraction_status_lock:
            if result:
                filepath = result
                extraction_status.update({
                    'results_file': filepath,
                    'progress': 100,
                    'message': "Extraction complete!",
                    'running': False
                })
                if output_format.lower() == 'csv':
                    asin = extractor.product_asin or 'unknown_asin'
                    timestamp = Path(filepath).stem.split('_')[-1]
                    summary_path = Path(app.config['REVIEWS_DIR']) / f"amazon_reviews_{asin}_{timestamp}_summary.json"
                    if summary_path.exists():
                        extraction_status['summary_file'] = str(summary_path)
                    else:
                        extraction_status['summary_file'] = None
                else:
                    extraction_status['summary_file'] = None  # Explicitly set to None for JSON output
            else:
                extraction_status.update({
                    'error': "Extraction failed or no reviews found",
                    'progress': 100,
                    'running': False
                })

    except Exception as e:
        with extraction_status_lock:
            extraction_status.update({
                'error': str(e),
                'running': False,
                'progress': 100
            })

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        product_url = request.form.get('product_url')
        num_pages = request.form.get('num_pages', type=int)
        output_format = request.form.get('output_format', 'json').lower()

        if output_format not in ['json', 'csv']:
            return render_template('index.html', error="Invalid output format. Choose 'json' or 'csv'.")

        if not product_url:
            return render_template('index.html', error="Product URL is required")

        with extraction_status_lock:
            global extraction_status
            extraction_status = {
                'running': True,
                'progress': 0,
                'message': 'Starting extraction...',
                'results_file': None,
                'summary_file': None,
                'product_url': product_url,
                'num_pages': num_pages,
                'error': None
            }

        thread = threading.Thread(
            target=run_extraction,
            args=(product_url, num_pages, output_format, app.debug)
        )
        thread.start()

        return redirect(url_for('loading'))

    return render_template('index.html')

@app.route('/loading')
def loading():
    return render_template('loading.html')

@app.route('/status')
def status():
    with extraction_status_lock:
        return jsonify(extraction_status)

@app.route('/download/<file_type>')
def download_file(file_type):
    with extraction_status_lock:
        filepath = extraction_status.get('results_file') if file_type == 'results' else extraction_status.get('summary_file')

    if not filepath or not Path(filepath).exists():
        return redirect(url_for('index'), code=302)

    return send_file(filepath, as_attachment=True, download_name=Path(filepath).name)

@app.route('/results')
def results():
    with extraction_status_lock:
        status = extraction_status.copy()

    if status.get('error'):
        return render_template('index.html', error=status['error'])

    if not status.get('results_file'):
        return redirect(url_for('index'))

    filepath = status['results_file']
    if not Path(filepath).exists():
        return render_template('index.html', error="Results file not found")

    results = {}
    summary = {}
    reviews = []

    analyzer = SentimentIntensityAnalyzer()

    if filepath.endswith('.json'):
        with open(filepath, 'r', encoding='utf-8') as f:
            results = json.load(f)
        reviews = results.get('reviews', [])
        summary = results.get('summary', {})
        sentiment_counts = results.get('sentiment_analysis', {'positive': 0, 'negative': 0, 'neutral': 0})
        summary_file = None  # JSON output doesn't have a separate summary file
    elif filepath.endswith('.csv'):
        import pandas as pd
        df = pd.read_csv(filepath, encoding='utf-8-sig')
        reviews = df.to_dict('records')
        summary_file = status.get('summary_file')
        if summary_file and Path(summary_file).exists():
            with open(summary_file, 'r', encoding='utf-8') as f:
                summary = json.load(f)
            # For CSV, use the review_counts from the summary file
            sentiment_counts = summary.get('review_counts', {'positive': 0, 'negative': 0, 'neutral': 0})
        else:
            summary_file = None
            sentiment_counts = {'positive': 0, 'negative': 0, 'neutral': 0}
        results = {
            'metadata': {
                'product_url': status.get('product_url', ''),
                'extraction_date': datetime.now().isoformat(),
                'total_reviews': len(df),
                'source': 'Amazon',
                'extractor_version': '1.1'
            },
            'reviews': reviews,
            'summary': summary
        }

    # Ensure all expected fields are present in reviews
    for review in reviews:
        review.setdefault('reviewer_name', 'Unknown')
        review.setdefault('rating', 0.0)
        review.setdefault('title', 'No Title')
        review.setdefault('date', 'Unknown Date')
        review.setdefault('body', '')
        review.setdefault('sentiment', 'neutral')

    # Step 1: Compute initial sentiment scores using VADER to rank reviews
    sentiment_scores = []
    for review in reviews:
        text = review.get("body", "")
        if not text:
            score = 0.0  # Neutral default for empty reviews
        else:
            scores = analyzer.polarity_scores(text)
            score = scores["compound"]
        sentiment_scores.append((review, score))

    # Step 2: Sort reviews by compound score (highest to lowest)
    sentiment_scores.sort(key=lambda x: x[1], reverse=True)

    # Step 3: Assign sentiments to match the counts from sentiment_analysis
    target_counts = {
        'positive': sentiment_counts['positive'],
        'negative': sentiment_counts['negative'],
        'neutral': sentiment_counts['neutral']
    }
    assigned_counts = {'positive': 0, 'negative': 0, 'neutral': 0}

    # Assign positive sentiments to the highest-scoring reviews
    for review, score in sentiment_scores:
        if assigned_counts['positive'] < target_counts['positive']:
            review['sentiment'] = 'positive'
            assigned_counts['positive'] += 1
        elif assigned_counts['negative'] < target_counts['negative'] and score < 0:
            review['sentiment'] = 'negative'
            assigned_counts['negative'] += 1
        else:
            # Assign remaining reviews as neutral or negative based on score
            if assigned_counts['negative'] < target_counts['negative'] and score < -0.05:
                review['sentiment'] = 'negative'
                assigned_counts['negative'] += 1
            else:
                review['sentiment'] = 'neutral'
                assigned_counts['neutral'] += 1

    # Ensure summary has all expected fields
    summary.setdefault('pros', ['No pros available'] * 5)
    summary.setdefault('cons', ['No cons available'] * 5)
    summary.setdefault('summary', ['No summary available'] * 5)
    summary.setdefault('total_score', 0)
    # Use the counts from sentiment_analysis
    summary['review_counts'] = {
        'positive': sentiment_counts['positive'],
        'negative': sentiment_counts['negative'],
        'neutral': sentiment_counts['neutral'],
        'total': len(reviews)
    }

    context = {
        'product_url': results['metadata']['product_url'],
        'total_reviews': results['metadata']['total_reviews'],
        'extraction_date': results['metadata']['extraction_date'],
        'reviews': reviews,
        'sentiment': sentiment_counts,
        'summary': summary,
        'summary_file': summary_file
    }

    return render_template('results.html', **context)

if __name__ == '__main__':
    # Use environment variables for port to work with Render
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)