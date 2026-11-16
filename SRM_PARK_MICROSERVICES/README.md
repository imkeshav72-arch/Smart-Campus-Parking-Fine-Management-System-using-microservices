SMART CAMPUS PARKING - NOW WITH MICROSERVICES
=============================================
Subject concept added: MICROSERVICE ARCHITECTURE (2 independent services + main app)

  Main App (Flask, :5000) --REST--> Payment Service      (:5001, own DB: payment_service.db)
                          --REST--> Notification Service (:5002, own DB: notification_service.db)
  Payment Service --REST--> Notification Service   (service-to-service call after each payment)

MICROSERVICE 1 - payment_service/  (Razorpay-style FAKE gateway)
  POST /api/orders          create order (Basic auth key_id:key_secret, amount in paise)
  GET  /api/orders/<id>     order status
  GET  /checkout/<id>       hosted checkout page: fake UPI QR tab + test card tab
  GET  /qr/<id>.png         the fake QR (opens a demo "UPI app" page; phone on same Wi-Fi can scan it)
  Result is signed with HMAC-SHA256 (order_id|payment_id) and verified by the main app,
  which ALSO confirms server-to-server with GET /api/orders/<id>.
  Test cards: 4111 1111 1111 1111 = success, 4000 0000 0000 0002 = declined.

MICROSERVICE 2 - notification_service/
  POST /api/notify          send email / sms / in-app message (X-Service-Token header)
  GET  /api/notifications   list stored messages
  GET  /                    live dashboard (auto refresh)

HOW TO RUN (all three together)
  python -m pip install -r requirements.txt
  python run_all.py            (or double-click run_windows.bat)
Always open http://127.0.0.1:5000 (use 127.0.0.1, not localhost, everywhere).

DEMO FLOW FOR TEACHER
 1. Admin login -> Microservices page: shows both services UP + architecture.
 2. Student: register, add vehicle. Guard reports offence, Admin approves -> fine created.
 3. Student -> My Fines -> Pay Fine -> "Pay Online (Demo Gateway)" -> redirected to Payment Service.
 4. Scan QR with phone (same Wi-Fi) or click "I have scanned - Pay", or use the Card tab.
 5. Student is redirected back, fine is Paid, receipt shown.
 6. Open http://127.0.0.1:5002 : Notification Service shows the email/in-app messages.
 7. Stop payment service (Ctrl+C its window) and try paying: main app shows a clean error
    (fault isolation - a microservice benefit).

NOTE: demo gateway only. No real money, never enter a real card.

----- ORIGINAL README -----
SMART CAMPUS PARKING – ADMIN, GUARD, QR AND FINE PAYMENTS

SETUP IN VS CODE (WINDOWS)
1. Extract this ZIP and open the folder `smart_campus_clean_booking` in VS Code.
2. Open Terminal in VS Code.
3. Run:
   python -m venv .venv
   .venv\Scripts\activate
   python -m pip install -r requirements.txt
4. Optional: copy `.env.example` to `.env` and add Razorpay TEST keys from your Razorpay Dashboard.
5. Run: python app.py
6. Open http://127.0.0.1:5000

DEMO ADMIN LOGIN
Email: admin@campus.edu
Password: admin123

DEMO GUARD LOGIN
Email: guard@campus.edu
Password: guard123

STUDENT
Register using official @srmist.edu.in email, add a vehicle, and book a parking slot. Student accounts, bookings, vehicles, offences, fines and payments are stored in the same database used by Admin and Guard dashboards.

GUARD FEATURES
- Live list of all vehicles currently marked Occupied (inside campus), not only today's booking list.
- Scan the student's booking QR using the camera or upload a QR image. Manual booking-code entry is also available.
- QR includes a booking code and random token; the server checks the token, booking date and status.
- Mark vehicle arrived (Occupied) and mark vehicle exited (Completed), which releases the slot.
- Report violations with a required photo, location and description. The report is sent to Admin for review; an approved report creates the student's fine.
- Camera scanning requires browser camera permission and localhost or HTTPS. If camera access is unavailable, use manual code entry or upload the QR image.

ADMIN FEATURES
- Dashboard counts for students, vehicles, bookings, offences, fines, pending fine amount, collected amount and restricted students.
- All registered SRMIST students and their bookings are visible in the Admin dashboard.
- Live list of vehicles currently inside campus.
- Review guard violation reports, inspect photo evidence, approve/reject and issue a fine.
- Configure fine amounts, review appeals, view payments and manage restricted students.
- Direct UPI QR references are manually verified against the real merchant bank statement before marking a fine paid.

PAYMENT – SIMPLE UPI QR ONLY
1. Copy `.env.example` and rename the copy to `.env`.
2. Open `.env` and replace `your-campus-upi@bank` with the REAL campus UPI ID.
   Example format: campusname@okaxis (use your actual ID, not this example).
3. Keep `.env` either in this `smart_campus_clean_booking` folder (same folder as app.py)
   OR in the parent project folder. The app checks both locations automatically.
4. Save `.env`, stop Flask with Ctrl+C, and restart with `python app.py`.
5. Student -> My Fines -> Pay Fine displays an amount-specific UPI QR.
6. Student scans using GPay/PhonePe/Paytm and submits the UTR. Admin verifies
   the transaction against the actual merchant bank statement before approving it.

The website does not collect UPI PIN, card number or CVV. A submitted UTR is not
proof by itself; the fine remains pending until Admin verifies and approves it.
A real campus UPI ID is required. Do not use a made-up ID.

IMPORTANT
- The project does not ask students to enter UPI PIN, card number or CVV in this website.
- Direct campus QR payments remain Pending until Admin checks the actual bank/merchant statement.
- Razorpay payments are marked Paid only after signature verification.
- If an older local database causes a schema error, stop Flask and back up `campus.db` before recreating it. Do not delete a database containing records you need.
