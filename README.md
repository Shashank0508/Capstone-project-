# Amazon Review Extractor

A web application that extracts and analyzes Amazon product reviews, providing sentiment analysis and summary insights.

## Features

- Extract reviews from Amazon product pages
- Analyze sentiment using VADER
- Generate summaries of pros and cons
- Interactive UI for filtering and viewing reviews
- Export results as JSON or CSV

## Deployment on Render

This project is configured for deployment on Render. Follow these steps to deploy:

### Prerequisites

- A [Render](https://render.com/) account
- An [OpenAI API key](https://platform.openai.com/account/api-keys) (for summary generation)

### Deployment Steps

1. **Fork or clone this repository**

2. **Create a new Web Service on Render**
   - Go to the Render dashboard and click "New +"
   - Select "Web Service"
   - Connect your GitHub repository
   - Name your service (e.g., "amazon-review-extractor")

3. **Configure the Web Service**
   - **Environment**: Docker
   - **Region**: Choose the region closest to your users
   - **Branch**: main (or your preferred branch)
   - **Build Command**: Leave empty (Docker handles this)
   - **Start Command**: Leave empty (Docker handles this)

4. **Set Environment Variables**
   - Click on "Environment" tab
   - Add the following environment variables:
     - `OPENAI_API_KEY`: Your OpenAI API key
     - `AMAZON_EMAIL`: (Optional) Your Amazon email if you need to handle login
     - `AMAZON_PASSWORD`: (Optional) Your Amazon password if you need to handle login

5. **Deploy**
   - Click "Create Web Service"
   - Render will build and deploy your application

### Important Notes for Render Deployment

- The application is configured to run in headless mode for Chrome, which is required for Render's containerized environment
- The app listens on port 8080 by default, but will use the `PORT` environment variable if provided by Render
- Persistent storage: Render's free tier doesn't provide persistent storage, so extracted reviews will be lost when the service restarts. Consider integrating with a database or cloud storage for production use.

## Local Development

### Setup

1. Clone the repository
2. Create a virtual environment: `python -m venv venv`
3. Activate the virtual environment:
   - Windows: `venv\Scripts\activate`
   - macOS/Linux: `source venv/bin/activate`
4. Install dependencies: `pip install -r requirements.txt`
5. Copy `.env.sample` to `.env` and fill in your credentials
6. Run the application: `python app.py`

### Docker

To run locally with Docker:

```bash
docker build -t amazon-review-extractor .
docker run -p 8080:8080 -e OPENAI_API_KEY=your_key amazon-review-extractor
```

## License

MIT
