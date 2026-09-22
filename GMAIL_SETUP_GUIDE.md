# Gmail App Password Setup Guide for OTP Authentication
# ════════════════════════════════════════════════════════════════════════════

## Step-by-Step Instructions

### 1. Enable 2-Factor Authentication (2FA)

**If you haven't already:**

1. Go to: https://myaccount.google.com/security
2. Under "Signing in to Google", click "2-Step Verification"
3. Follow the prompts to enable 2FA
4. You'll need to verify your phone number

**Note:** You CANNOT generate an app password without 2FA enabled.

---

### 2. Generate App Password

1. Go to: https://myaccount.google.com/apppasswords
2. You may need to sign in again
3. Select app: **Mail**
4. Select device: **Windows Computer** (or Other)
5. Click **Generate**
6. Google will show a 16-character password like: `abcd efgh ijkl mnop`
7. **Copy this password immediately** - you won't be able to see it again

---

### 3. Update Your .env File

Open: `C:\Users\HARSHA B S\Desktop\carbon-scheduler\.env`

Replace these lines:
```env
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your-app-password-here
```

With your actual values:
```env
SMTP_USER=your-actual-email@gmail.com
SMTP_PASSWORD=abcd efgh ijkl mnop
```

**Important:**
- Remove spaces from the app password (or keep them - both work)
- Use your FULL Gmail address (e.g., username@gmail.com)
- Do NOT use your regular Gmail password - use the App Password

---

### 4. Save and Test

1. Save the `.env` file
2. Open: http://localhost:8501
3. Enter your Gmail address
4. Click "Send Verification Code"
5. Check your Gmail inbox for the OTP email
6. Enter the 6-digit code
7. Success! Dashboard should load

---

## Troubleshooting

### "Authentication failed"
- Make sure you're using the App Password, not your regular password
- Verify 2FA is enabled on your Google account
- Check that SMTP_USER matches the Gmail account exactly

### "Connection refused" or "Timeout"
- Check SMTP_HOST is exactly: `smtp.gmail.com`
- Check SMTP_PORT is exactly: `587`
- Verify your network allows outbound connections on port 587

### "Email not received"
- Check spam/junk folder
- Verify the email address is correct
- Wait 1-2 minutes (email delivery can be delayed)

### "Too many verification requests"
- You've hit the rate limit (3 requests per 10 minutes)
- Wait 10 minutes and try again

---

## Security Notes

✅ App passwords are MORE secure than your regular password
✅ They can be revoked at any time from Google Account settings
✅ They only work for the specific app/device you designated
✅ Your `.env` file is in `.gitignore` - it won't be committed to git
✅ Never share your app password or commit it to version control

---

## What If I Don't Have Gmail?

### For Outlook/Hotmail:
```env
SMTP_HOST=smtp-mail.outlook.com
SMTP_PORT=587
SMTP_USER=your-email@outlook.com
SMTP_PASSWORD=your-outlook-password
```

### For Yahoo:
```env
SMTP_HOST=smtp.mail.yahoo.com
SMTP_PORT=587
SMTP_USER=your-email@yahoo.com
SMTP_PASSWORD=your-yahoo-app-password
```

---

## Current Configuration Status

**Location:** `C:\Users\HARSHA B S\Desktop\carbon-scheduler\.env`

**Required Changes:**
- [ ] Replace `your-email@gmail.com` with your actual Gmail address
- [ ] Replace `your-app-password-here` with the 16-character app password from Google
- [ ] Save the file
- [ ] Test at http://localhost:8501

**Dashboard Status:** ✅ Running and ready at http://localhost:8501
**Waiting for:** Your Gmail credentials to be configured

---

Once you've completed these steps, the authentication system will work end-to-end.
