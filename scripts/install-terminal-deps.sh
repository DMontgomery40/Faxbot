#!/bin/bash
# Install dependencies for terminal feature in Faxbot Admin Console

set -e

echo "==================================="
echo "Faxbot Terminal Feature Installation"
echo "==================================="
echo ""

# Check if we're in the right directory
if [ ! -f "api/requirements.txt" ]; then
    echo "Error: Must run from faxbot root directory"
    exit 1
fi

echo "Installing Python dependencies..."
cd api
pip install websockets==12.0 pexpect==4.9.0 || {
    echo "Failed to install Python packages. Try: pip install -r requirements.txt"
}
cd ..

echo ""
echo "Installing Node.js dependencies for Admin UI..."
cd api/admin_ui
npm install || {
    echo "Failed to install Node packages. Try: npm install"
}
cd ../..

echo ""
echo "==================================="
echo "Terminal feature installation complete!"
echo "==================================="
echo ""
echo "To use the terminal:"
echo "1. Start the Faxbot API server:"
echo "   docker-compose up -d"
echo "   OR"
echo "   cd api && uvicorn app.main:app --host 0.0.0.0 --port 8080"
echo ""
echo "2. Access the Admin Console at http://localhost:8080/admin"
echo ""
echo "3. Login with your API key"
echo ""
echo "4. Click on the 'Terminal' tab"
echo ""
echo "The terminal provides direct shell access to the container/server"
echo "No additional authentication needed beyond the admin login!"
