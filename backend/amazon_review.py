import logging
import time
import random
import json
import pandas as pd
import sys
import os
import re
from datetime import datetime
import openai
from tenacity import retry, wait_fixed, stop_after_attempt
from pathlib import Path
from urllib.parse import quote
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, 
    NoSuchElementException, 
    WebDriverException,
    ElementClickInterceptedException,
    StaleElementReferenceException
)
from typing import Optional, Dict, List
from dotenv import load_dotenv
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# --- Configuration ---
LOG_DIR = "logs"
REVIEWS_DIR = "reviews"
WAIT_TIMEOUT_SECONDS = 20
MAX_NAVIGATION_RETRIES = 5
openai.api_key = os.getenv("OPENAI_API_KEY")

class AmazonReviewExtractor:
    def __init__(self):
        self.driver = None
        self.wait = None
        self.review_data = []
        self.logger = self.setup_logger()
        load_dotenv()
        self.amazon_email = os.getenv('AMAZON_EMAIL')
        self.amazon_password = os.getenv('AMAZON_PASSWORD')
        self.product_url = None  # Will be set dynamically via frontend
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0',
            'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/117.0'
        ]
        self.product_asin = None

    def setup_logger(self):
        log_dir_path = Path(__file__).parent / LOG_DIR
        log_dir_path.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = log_dir_path / f"amazon_extractor_{timestamp}.log"

        logger = logging.getLogger("AmazonExtractorLogger")
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            # File handler with UTF-8 encoding
            file_handler = logging.FileHandler(log_filename, encoding='utf-8')
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

            # Console handler with UTF-8 encoding
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            if sys.platform.startswith('win'):
                sys.stdout.reconfigure(encoding='utf-8')
            logger.addHandler(console_handler)

        return logger

    def setup_driver(self, debug_mode=True):
        try:
            self.logger.info("Setting up WebDriver...")
            chrome_options = Options()
            chrome_options.add_argument('--disable-blink-features=AutomationControlled')
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            chrome_options.add_argument(f'--user-agent={random.choice(self.user_agents)}')
            chrome_options.add_argument('--window-size=1920,1080')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--enable-unsafe-swiftshader')
            chrome_options.add_argument('--start-maximized')
            
            # Always run in headless mode for Render deployment
            chrome_options.add_argument('--headless=new')
            chrome_options.add_argument('--ignore-certificate-errors')
            chrome_options.add_argument('--ignore-ssl-errors')
            chrome_options.add_argument('--disable-application-cache')
            chrome_options.add_argument('--disable-extensions')
            if not debug_mode:
                chrome_options.add_argument('--headless=new')
                chrome_options.add_argument('--blink-settings=imagesEnabled=false')
                chrome_options.add_argument('--disable-software-rasterizer')

            # Use the pre-installed ChromeDriver in Docker container
            try:
                # Check if we're in the Docker container (pre-installed chromedriver)
                if os.path.exists('/usr/local/bin/chromedriver'):
                    self.logger.info("Using pre-installed ChromeDriver in Docker container")
                    service = Service(executable_path='/usr/local/bin/chromedriver')
                else:
                    # Fallback for local development
                    self.logger.info("Using local ChromeDriver")
                    # For Windows
                    if os.name == 'nt':
                        chrome_driver_path = os.path.join(os.getcwd(), 'chromedriver.exe')
                    else:
                        # For Linux/Mac
                        chrome_driver_path = os.path.join(os.getcwd(), 'chromedriver')
                    
                    if not os.path.exists(chrome_driver_path):
                        self.logger.warning(f"ChromeDriver not found at {chrome_driver_path}")
                        self.logger.info("Please download ChromeDriver manually for local development")
                        chrome_driver_path = 'chromedriver'  # Try using from PATH
                    
                    service = Service(executable_path=chrome_driver_path)
            except Exception as e:
                self.logger.error(f"Error setting up ChromeDriver service: {str(e)}")
                return False
            try:
                self.driver = webdriver.Chrome(service=service, options=chrome_options)
                self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                self.driver.execute_cdp_cmd('Network.setUserAgentOverride', {"userAgent": random.choice(self.user_agents)})
                self.wait = WebDriverWait(self.driver, WAIT_TIMEOUT_SECONDS)
                self.logger.info("WebDriver setup completed successfully")
                return True
            except Exception as e:
                self.logger.error(f"Failed to start ChromeDriver: {str(e)}")
                # Check if it's a version mismatch error
                if "session not created" in str(e) and "This version of ChromeDriver only supports Chrome version" in str(e):
                    self.logger.error("ChromeDriver version doesn't match Chrome browser version.")
                    self.logger.error("Please update the CHROMEDRIVER_VERSION in the Dockerfile to match your Chrome version.")
                return False
        except Exception as e:
            self.logger.error(f"Error setting up WebDriver: {str(e)}", exc_info=True)
            if "WinError 193" in str(e):
                self.logger.error(
                    "WinError 193 detected: This indicates an architecture mismatch. "
                    "Since your system is 64-bit, ensure you are using 64-bit Python and 64-bit Google Chrome. "
                    "Also, verify that ChromeDriver matches your Chrome version."
                )
            return False

    def analyze_sentiment(self, reviews: List[Dict]) -> Dict[str, int]:
        analyzer = SentimentIntensityAnalyzer()
        sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0}
        for review in reviews:
            text = review.get("body", "")
            if not text:
                continue
            scores = analyzer.polarity_scores(text)
            compound_score = scores["compound"]
            if compound_score >= 0.05:
                sentiment_counts["positive"] += 1
            elif compound_score <= -0.05:
                sentiment_counts["negative"] += 1
            else:
                sentiment_counts["neutral"] += 1
        self.logger.info(f"Sentiment analysis: {sentiment_counts}")
        return sentiment_counts

    def save_reviews(self, reviews: List[Dict]) -> str:
        asin = self.product_asin if self.product_asin else "unknown_asin"
        output_data = {
            "metadata": {
                "product_url": self.product_url,
                "extraction_date": datetime.now().isoformat(),
                "total_reviews": len(reviews),
                "source": "Amazon",
                "extractor_version": "1.1"
            },
            "reviews": reviews,
            "sentiment_analysis": self.analyze_sentiment(reviews)
        }

        output_path = Path(REVIEWS_DIR) / f"amazon_reviews_{asin}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        output_path.parent.mkdir(exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        self.logger.info(f"Reviews saved to: {output_path} with sentiment analysis: {output_data['sentiment_analysis']}")
        return str(output_path)

    def _safe_get(self, url: str, description: str) -> bool:
        for attempt in range(MAX_NAVIGATION_RETRIES):
            try:
                self.logger.info(f"Attempt {attempt + 1}/{MAX_NAVIGATION_RETRIES}: Navigating to {description} URL: {url}")
                self.driver.get(url)
                time.sleep(random.uniform(3, 5))
                current_url = self.driver.current_url.lower()

                if "404" in current_url or "document not found" in self.driver.page_source.lower():
                    self.logger.warning(f"404 error detected on {description} URL: {current_url}")
                    if attempt < MAX_NAVIGATION_RETRIES - 1:
                        time.sleep(random.uniform(4, 8))
                        continue
                    else:
                        self.logger.error(f"Failed to navigate to {description} URL after {MAX_NAVIGATION_RETRIES} attempts due to 404.")
                        return False

                if 'captcha' in current_url or 'ap/challenge' in current_url:
                    self.logger.warning("CAPTCHA or challenge page detected during navigation.")
                    try:
                        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.a-row.a-text-center img")))
                        self.logger.error(
                            "CAPTCHA detected, but this application cannot handle interactive CAPTCHA solving in a web context. "
                            "Please try again later or use a different product URL."
                        )
                        raise RuntimeError("CAPTCHA detected; manual intervention required but not possible in this context.")
                    except TimeoutException:
                        self.logger.error("CAPTCHA expected but not found within timeout.")
                        return False

                try:
                    if "reviews" in description.lower():
                        review_selectors = [
                            'div[data-hook="review"]',
                            'div.a-section.review.aok-relative',
                            '#cm_cr-review_list',
                            '.review',
                        ]

                        found = False
                        for selector in review_selectors:
                            try:
                                self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                                self.logger.info(f"Successfully verified reviews page using selector: {selector}")
                                found = True
                                break
                            except TimeoutException:
                                continue

                        if found:
                            return True

                        if "reviews" in self.driver.current_url.lower() or "review" in self.driver.current_url.lower():
                            self.logger.info(f"On reviews page based on URL, though review elements not found. URL: {self.driver.current_url}")
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            screenshot_path = f"reviews/reviews_page_{timestamp}.png"
                            self.driver.save_screenshot(screenshot_path)
                            self.logger.info(f"Saved screenshot to {screenshot_path}")
                            return True

                        self.logger.warning("Could not verify reviews page with any known selectors.")
                        return False

                except TimeoutException:
                    self.logger.warning(f"Failed to verify {description} page load. Current URL: {self.driver.current_url}")
                    if attempt < MAX_NAVIGATION_RETRIES - 1:
                        time.sleep(random.uniform(4, 8))
                    else:
                        self.logger.error(f"Failed to navigate to {description} URL after {MAX_NAVIGATION_RETRIES} attempts.")
                        return False

            except (TimeoutException, WebDriverException) as e:
                self.logger.warning(f"Attempt {attempt + 1} failed to navigate to {description} URL: {e}")
                if attempt < MAX_NAVIGATION_RETRIES - 1:
                    time.sleep(random.uniform(4, 8))
                else:
                    self.logger.error(f"Failed to navigate to {description} URL after {MAX_NAVIGATION_RETRIES} attempts.")
                    return False

            except Exception as e:
                self.logger.error(f"An unexpected error occurred during navigation to {description} URL: {e}", exc_info=True)
                return False

    def extract_asin_from_url(self, url: str) -> Optional[str]:
        try:
            patterns = [
                r'/dp/([A-Z0-9]{10})',
                r'/gp/product/([A-Z0-9]{10})',
                r'/product/([A-Z0-9]{10})',
                r'/ASIN/([A-Z0-9]{10})',
                r'/([A-Z0-9]{10})(?:[/?]|$)'
            ]
            for pattern in patterns:
                match = re.search(pattern, url, re.IGNORECASE)
                if match:
                    return match.group(1).upper()
            return None
        except Exception as e:
            self.logger.error(f"Error extracting ASIN: {str(e)}")
            return None

    def get_reviews_url(self) -> str:
        try:
            asin = self.extract_asin_from_url(self.product_url)
            if not asin:
                self.logger.error(f"Could not extract ASIN from product URL: {self.product_url}")
                return None
            reviews_url = f"https://www.amazon.in/product-reviews/{asin}"
            self.logger.info(f"Generated reviews URL: {reviews_url}")
            return reviews_url
        except Exception as e:
            self.logger.error(f"Error generating reviews URL: {str(e)}")
            return None

    def extract_review_data(self, review_element) -> Dict:
        try:
            review_id = review_element.get_attribute('id') or f"review_{random.randint(10000, 99999)}"
            
            reviewer_name = "Unknown"
            try:
                reviewer_name_elem = review_element.find_element(By.CSS_SELECTOR, '.a-profile-name')
                reviewer_name = reviewer_name_elem.text.strip() if reviewer_name_elem else "Unknown"
            except NoSuchElementException:
                pass
            
            rating = 0.0
            try:
                rating_elem = review_element.find_element(By.CSS_SELECTOR, 'i[data-hook="review-star-rating"]')
                rating_text = rating_elem.get_attribute('textContent').strip()
                rating = float(rating_text.split()[0]) if rating_text else 0.0
            except (NoSuchElementException, ValueError, AttributeError):
                pass
            
            title = "No Title"
            body = ""
            try:
                title_elem = review_element.find_element(By.CSS_SELECTOR, 'a[data-hook="review-title"]')
                title = title_elem.text.strip() if title_elem else "No Title"
                body_elem = review_element.find_element(By.CSS_SELECTOR, 'span[data-hook="review-body"] span')
                body = body_elem.text.strip() if body_elem else ""
            except NoSuchElementException:
                pass
            
            date = ""
            verified = False
            try:
                date_elem = review_element.find_element(By.CSS_SELECTOR, 'span[data-hook="review-date"]')
                date_text = date_elem.text.strip() if date_elem else ""
                date = re.search(r'on\s+(.+)', date_text).group(1).strip() if date_text and re.search(r'on\s+(.+)', date_text) else ""
                verified_elem = review_element.find_element(By.CSS_SELECTOR, 'span[data-hook="avp-badge"]')
                verified = "Verified Purchase" in verified_elem.text.strip() if verified_elem else False
            except NoSuchElementException:
                pass
            
            helpful_votes = 0
            try:
                votes_elem = review_element.find_element(By.CSS_SELECTOR, 'span[data-hook="helpful-vote-statement"]')
                votes_text = votes_elem.text.strip() if votes_elem else ""
                votes_match = re.search(r'(\d+)', votes_text)
                helpful_votes = int(votes_match.group(1)) if votes_match else 0
            except NoSuchElementException:
                pass
            
            images = []
            try:
                image_elems = review_element.find_elements(By.CSS_SELECTOR, 'img[data-hook="review-image-tile"]')
                images = [img.get_attribute('src') for img in image_elems if img.get_attribute('src')]
            except NoSuchElementException:
                pass
            
            country = ""
            try:
                country_elem = review_element.find_element(By.CSS_SELECTOR, 'span[data-hook="review-date"]')
                if country_elem:
                    country_text = country_elem.text.strip()
                    country_match = re.search(r'in\s+(.+?)\s+on', country_text)
                    country = country_match.group(1).strip() if country_match else ""
            except NoSuchElementException:
                pass
            
            variant = ""
            try:
                variant_elem = review_element.find_element(By.CSS_SELECTOR, 'a[data-hook="format-strip"]')
                variant = variant_elem.text.strip() if variant_elem else ""
            except NoSuchElementException:
                pass
            
            return {
                'review_id': review_id,
                'reviewer_name': reviewer_name,
                'rating': rating,
                'title': title,
                'date': date,
                'country': country,
                'verified_purchase': verified,
                'product_variant': variant,
                'body': body,
                'helpful_votes': helpful_votes,
                'images': images,
                'extracted_at': datetime.now().isoformat()
            }
        except Exception as e:
            self.logger.error(f"Error extracting review data: {str(e)}")
            return {
                'review_id': f"error_{random.randint(10000, 99999)}",
                'error': str(e),
                'extracted_at': datetime.now().isoformat()
            }

    def extract_reviews_from_page(self) -> List[Dict]:
        reviews = []
        try:
            review_selectors = [
                'div[data-hook="review"]',
                'div.a-section.review.aok-relative',
                '#cm_cr-review_list div[data-hook="review"]',
                '.review',
            ]
            
            review_elements = []
            for selector in review_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        self.logger.info(f"Found {len(elements)} reviews using selector: {selector}")
                        review_elements = elements
                        break
                except Exception as e:
                    self.logger.debug(f"Selector {selector} failed: {str(e)}")
            
            if not review_elements:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_path = f"no_reviews_found_{timestamp}.png"
                self.driver.save_screenshot(screenshot_path)
                self.logger.warning(f"No reviews found on current page. Saved screenshot to {screenshot_path}")
                if "review" in self.driver.current_url.lower():
                    with open(f"review_page_source_{timestamp}.html", "w", encoding="utf-8") as f:
                        f.write(self.driver.page_source)
                    self.logger.info(f"Saved page source to review_page_source_{timestamp}.html")
                return []
            
            self.logger.info(f"Found {len(review_elements)} reviews on current page")
            
            for review_elem in review_elements:
                try:
                    review_data = self.extract_review_data(review_elem)
                    reviews.append(review_data)
                    try:
                        self.logger.info(f"Extracted review: {review_data['title']}")
                    except UnicodeEncodeError as e:
                        self.logger.error(f"Failed to log review title due to encoding issue: {str(e)}")
                        self.logger.info(f"Extracted review: [Title contained non-ASCII characters]")
                except Exception as e:
                    self.logger.error(f"Error processing review: {str(e)}")
            
            return reviews
        except TimeoutException:
            self.logger.warning("No reviews found on current page")
            return []
        except Exception as e:
            self.logger.error(f"Error extracting reviews from page: {str(e)}")
            return []

    def has_next_page(self) -> bool:
        try:
            next_page_selectors = [
                'li.a-last a',
                'a[data-hook="pagination-next"]',
                'span.a-last a',
            ]
            for selector in next_page_selectors:
                try:
                    next_button = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if next_button.is_enabled():
                        return True
                except NoSuchElementException:
                    continue
                    
            disabled_selectors = [
                'li.a-disabled.a-last',
                'span.a-last.a-disabled',
            ]
            for selector in disabled_selectors:
                try:
                    self.driver.find_element(By.CSS_SELECTOR, selector)
                    self.logger.info("Found disabled next button - we're on the last page")
                    return False
                except NoSuchElementException:
                    continue
            
            return False
        except Exception as e:
            self.logger.error(f"Error checking for next page: {str(e)}")
            return False

    def goto_next_page(self) -> bool:
        try:
            current_url = self.driver.current_url
            self.logger.debug(f"Current page URL: {current_url}")

            next_page_selectors = [
                'a[aria-label="Next page"]',
                '#cm_cr-pagination_bar a[title="Next page"]',
                'li.a-last a',
                '.a-last a',
                'a[data-hook="pagination-bar"]',
            ]

            for selector in next_page_selectors:
                try:
                    next_button = self.wait.until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                    self.logger.debug(f"Found next page button using selector: {selector}")
                    
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", next_button)
                    time.sleep(random.uniform(1, 2))
                    
                    self.driver.execute_script("arguments[0].click();", next_button)
                    time.sleep(random.uniform(3, 5))

                    new_url = self.driver.current_url
                    if new_url == current_url:
                        self.logger.warning("Page URL did not change after clicking 'Next' button. Assuming end of pagination.")
                        return False

                    self.logger.info("Successfully navigated to next page")
                    return True

                except (TimeoutException, WebDriverException) as e:
                    self.logger.debug(f"Next page selector {selector} failed: {str(e)}")
                    continue

            self.logger.info("No 'Next' button found. End of pagination.")
            return False

        except Exception as e:
            self.logger.error(f"Error navigating to next page: {str(e)}")
            return False

    def extract_all_reviews(self, max_pages=None) -> List[Dict]:
        all_reviews = []
        seen_review_ids = set()
        page_num = 1
        
        reviews_url = self.get_reviews_url()
        if not reviews_url:
            self.logger.error("Failed to generate reviews URL")
            return all_reviews
        
        if not self._safe_get(reviews_url, "product reviews page"):
            self.logger.error("Failed to navigate to reviews page")
            return all_reviews
        
        while True:
            self.logger.info(f"Extracting reviews from page {page_num}")
            
            page_reviews = self.extract_reviews_from_page()
            initial_count = len(all_reviews)
            
            for review in page_reviews:
                review_id = review.get('review_id')
                if review_id and review_id not in seen_review_ids:
                    seen_review_ids.add(review_id)
                    all_reviews.append(review)
                else:
                    self.logger.debug(f"Skipped duplicate review with ID: {review_id}")
            
            duplicates_removed = len(page_reviews) - (len(all_reviews) - initial_count)
            self.logger.info(f"Extracted {len(page_reviews)} reviews from page {page_num}, {duplicates_removed} duplicates removed. Total unique reviews: {len(all_reviews)}")
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = f"reviews/reviews_page_{page_num}_{timestamp}.png"
            self.driver.save_screenshot(screenshot_path)
            self.logger.info(f"Saved screenshot to {screenshot_path}")
            
            if max_pages and page_num >= max_pages:
                self.logger.info(f"Reached maximum page limit ({max_pages})")
                break
            
            if not self.has_next_page():
                self.logger.info("No more review pages available")
                break
                
            if not self.goto_next_page():
                self.logger.error("Failed to navigate to next page")
                break
                
            page_num += 1
            time.sleep(random.uniform(2, 5))
        
        return all_reviews

    def save_reviews_csv(self, reviews: List[Dict], filename=None) -> str:
        try:
            reviews_dir = Path(REVIEWS_DIR)
            reviews_dir.mkdir(exist_ok=True)
            
            if not filename:
                asin = self.extract_asin_from_url(self.product_url)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"amazon_reviews_{asin or 'product'}_{timestamp}.csv"
            
            filepath = reviews_dir / filename
            
            columns = [
                'review_id', 'reviewer_name', 'rating', 'title', 'body',
                'date', 'country', 'verified_purchase', 'product_variant',
                'helpful_votes', 'images', 'extracted_at'
            ]
            
            df = pd.DataFrame(reviews)
            if 'images' in df.columns:
                df['images'] = df['images'].apply(lambda x: ' | '.join(x) if isinstance(x, list) else '')
            
            df = df.reindex(columns=columns)
            df.to_csv(filepath, index=False, encoding='utf-8-sig')
            
            self.logger.info(f"Saved {len(reviews)} reviews to CSV: {filepath}")
            return str(filepath)
            
        except Exception as e:
            self.logger.error(f"Error saving reviews to CSV: {str(e)}")
            return None

    @retry(wait=wait_fixed(2), stop=stop_after_attempt(3))
    def generate_summary(self, reviews):
        review_text = "\n".join([f"Rating: {r['rating']}, Review: {r['body']}" for r in reviews if r['body']])
        if not review_text:
            self.logger.warning("No review text available for summary generation")
            return {
                "pros": ["No pros available"] * 5,
                "cons": ["No cons available"] * 5,
                "summary": ["No summary available"] * 5,
                "review_counts": {"positive": 0, "negative": 0, "neutral": 0, "total": 0},
                "total_score": 0
            }

        try:
            prompt = f"""
            Based on the following Amazon reviews, generate:
            1. 5 pros about the product.
            2. 5 cons about the product.
            3. A 5-line summary of the product based on the reviews.

            Reviews:
            {review_text}

            Return the response in JSON format with keys 'pros', 'cons', and 'summary', where 'pros' and 'cons' are lists of 5 items each, and 'summary' is a list of 5 strings.
            """
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.7
            )
            result = json.loads(response.choices[0].message.content.strip())

            pros = result.get("pros", ["No pros available"] * 5)[:5]
            cons = result.get("cons", ["No cons available"] * 5)[:5]
            summary = result.get("summary", ["No summary available"] * 5)[:5]
            if len(pros) < 5: pros.extend(["No pros available"] * (5 - len(pros)))
            if len(cons) < 5: cons.extend(["No cons available"] * (5 - len(cons)))
            if len(summary) < 5: summary.extend(["No summary available"] * (5 - len(summary)))

            sentiment_counts = self.analyze_sentiment(reviews)
            total_reviews = len(reviews)
            average_rating = sum(r['rating'] for r in reviews if r.get('rating')) / total_reviews if total_reviews > 0 else 0
            total_score = min(100, max(0, int(average_rating * 20)))

            return {
                "pros": pros,
                "cons": cons,
                "summary": summary,
                "review_counts": sentiment_counts | {"total": total_reviews},
                "total_score": total_score
            }
        except Exception as e:
            self.logger.error(f"Error generating summary with OpenAI: {str(e)}. Returning default values.")
            return {
                "pros": ["No pros available"] * 5,
                "cons": ["No cons available"] * 5,
                "summary": ["No summary available"] * 5,
                "review_counts": {"positive": 0, "negative": 0, "neutral": 0, "total": 0},
                "total_score": 0
            }

    def run(self, product_url=None, max_pages=None, save_format="json", debug=True):
        try:
            self.logger.info("Starting Amazon Review Extractor")
            if not product_url:
                self.logger.error("Product URL not provided")
                return None
            if not product_url.startswith("https://www.amazon"):
                self.logger.error("Invalid product URL: Must be an Amazon URL")
                return None
            self.product_url = product_url
            self.product_asin = self.extract_asin_from_url(self.product_url)
            if not self.product_asin:
                self.logger.warning("Could not extract ASIN from product URL, using 'unknown_asin' as fallback")
                self.product_asin = "unknown_asin"

            if not self.setup_driver(debug_mode=debug):
                self.logger.error("Failed to set up WebDriver")
                return None

            reviews = self.extract_all_reviews(max_pages)
            self.logger.info(f"Total unique reviews extracted: {len(reviews)}")
            
            summary = self.generate_summary(reviews) if reviews else {
                "pros": ["No pros available"] * 5,
                "cons": ["No cons available"] * 5,
                "summary": ["No summary available"] * 5,
                "review_counts": {"positive": 0, "negative": 0, "neutral": 0, "total": 0},
                "total_score": 0
            }

            if save_format.lower() == "csv":
                filepath = self.save_reviews_csv(reviews)
                if reviews:
                    asin = self.product_asin if self.product_asin else "unknown_asin"
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    summary_path = Path(REVIEWS_DIR) / f"summary_{asin}_{timestamp}.json"
                    with open(summary_path, 'w', encoding='utf-8') as f:
                        json.dump(summary, f, ensure_ascii=False, indent=2)
                    self.logger.info(f"Saved summary to {summary_path}")
            else:
                filepath = self.save_reviews(reviews)
                if reviews and filepath:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    data["summary"] = summary
                    with open(filepath, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    self.logger.info(f"Updated {filepath} with summary")

            if filepath:
                self.logger.info(f"Reviews saved to: {filepath}")
            return filepath if len(reviews) > 0 else None

        except Exception as e:
            self.logger.error(f"Unhandled exception in run method: {str(e)}", exc_info=True)
            return None
        finally:
            self.close()

    def close(self):
        if hasattr(self, 'driver') and self.driver is not None:
            try:
                self.driver.quit()
                self.logger.info("WebDriver closed successfully")
            except Exception as e:
                self.logger.error(f"Error closing WebDriver: {e}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract Amazon product reviews")
    parser.add_argument("--product-url", type=str, help="Amazon product URL to extract reviews from")
    parser.add_argument("--max-pages", type=int, default=None, help="Maximum number of review pages to extract")
    parser.add_argument("--format", type=str, choices=["json", "csv"], default="json", help="Output file format")
    parser.add_argument("--debug", action="store_true", help="Run in debug mode with visible browser")
    args = parser.parse_args()
    
    extractor = AmazonReviewExtractor()
    try:
        filepath = extractor.run(
            product_url=args.product_url,
            max_pages=args.max_pages,
            save_format=args.format,
            debug=args.debug
        )
        if filepath:
            print(f"Review extraction completed successfully. Output saved to: {filepath}")
        else:
            print("Review extraction failed or no data found")
    except KeyboardInterrupt:
        print("\nProcess interrupted by user")
    finally:
        extractor.close()