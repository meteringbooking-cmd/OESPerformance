import json
import os
import time
import requests
import re
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore
from http.server import BaseHTTPRequestHandler

firebase_config_str = os.environ.get('FIREBASE_CONFIG_JSON')

if firebase_config_str and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_config_str)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        print(f"Firebase failed to init: {e}")

db = firestore.client() if firebase_admin._apps else None

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*') 
        self.end_headers()

        try:
            if not db:
                raise Exception("Firebase DB not connected. Ensure FIREBASE_CONFIG_JSON is set in Vercel.")

            price_changes = []
            
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            
            login_url = "https://secure.thinventory.com/Account/Login"
            login_page = session.get(login_url, timeout=10)
            soup_login = BeautifulSoup(login_page.text, 'html.parser')
            
            token_input = soup_login.find('input', {'name': '__RequestVerificationToken'})
            token = token_input['value'] if token_input else ''

            login_data = {
                "UserName": "Scott.coyle9072",
                "Password": "Mollielou16!",
                "__RequestVerificationToken": token,
                "RememberMe": "false"
            }
            
            post_resp = session.post(login_url, data=login_data, allow_redirects=True, timeout=10)
            
            inventory_ref = db.collection('Energise Inventory').stream()
            
            for doc in inventory_ref:
                product = doc.to_dict()
                sku = product.get('sku')
                current_price = product.get('price', 0.0)
                
                if not sku:
                    continue

                search_url = f"https://secure.thinventory.com/OrderManagement?q={sku}"
                response = session.get(search_url, timeout=10)
                
                soup = BeautifulSoup(response.text, 'html.parser')
                
                if "Login" in response.url:
                    raise Exception("Session expired or authentication blocked by Thinventory.")
                
                body_text = soup.get_text(separator=' ', strip=True)
                
                if str(sku) in body_text:
                    prices_found = re.findall(r'£\s*(\d+(?:\.\d{2})?)', body_text)
                    
                    if prices_found:
                        live_price = float(prices_found[0])
                        
                        if live_price != current_price:
                            price_changes.append(f"SKU {sku}: £{current_price} -> £{live_price}")
                            db.collection('Energise Inventory').document(doc.id).update({
                                'price': live_price
                            })
                            
                time.sleep(1)
            
            response_data = {"status": "success", "changes": price_changes}
            self.wfile.write(json.dumps(response_data).encode('utf-8'))

        except Exception as e:
            error_data = {"status": "error", "message": str(e)}
            self.wfile.write(json.dumps(error_data).encode('utf-8'))
