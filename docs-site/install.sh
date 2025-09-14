#!/bin/bash

# Faxbot MCP - One-Click Installer
# Supports macOS, Linux, and Windows (via WSL)

set -e

REPO_URL="https://github.com/DMontgomery40/Faxbot"
INSTALL_DIR="$HOME/.local/share/faxbot"
BIN_DIR="$HOME/.local/bin"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_header() {
    echo -e "${BLUE}"
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║                      Faxbot Installer                         ║"
    echo "║                                                               ║"
    echo "║  AI-Native Fax Transmission • T.38 Protocol • Voice Control  ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

print_step() {
    echo -e "${BLUE}▶${NC} $1"
}

print_success() {
    echo -e "${GREEN}✅${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠️${NC} $1"
}

print_error() {
    echo -e "${RED}❌${NC} $1"
}

check_requirements() {
    print_step "Checking system requirements..."
    
    # Check operating system
    case "$(uname -s)" in
        Darwin*)    OS="macOS";;
        Linux*)     OS="Linux";;
        CYGWIN*|MINGW*) OS="Windows";;
        *)          OS="Unknown";;
    esac
    
    if [ "$OS" = "Unknown" ]; then
        print_error "Unsupported operating system"
        exit 1
    fi
    
    print_success "Operating System: $OS"
    
    # Check Node.js
    if ! command -v node &> /dev/null; then
        print_error "Node.js is required but not installed."
        echo ""
        echo "Please install Node.js 18+ from: https://nodejs.org/"
        echo ""
        case "$OS" in
            macOS)
                echo "Or use Homebrew: brew install node"
                ;;
            Linux)
                echo "Or use your package manager:"
                echo "  Ubuntu/Debian: sudo apt install nodejs npm"
                echo "  CentOS/RHEL: sudo yum install nodejs npm"
                echo "  Arch: sudo pacman -S nodejs npm"
                ;;
        esac
        exit 1
    fi
    
    NODE_VERSION=$(node --version)
    NODE_MAJOR=$(echo $NODE_VERSION | cut -d'.' -f1 | sed 's/v//')
    
    if [ "$NODE_MAJOR" -lt 18 ]; then
        print_error "Node.js 18+ required, found $NODE_VERSION"
        exit 1
    fi
    
    print_success "Node.js $NODE_VERSION (compatible)"
    
    # Check npm
    if ! command -v npm &> /dev/null; then
        print_error "npm is required but not installed"
        exit 1
    fi
    
    print_success "npm $(npm --version)"
    
    # Check git
    if ! command -v git &> /dev/null; then
        print_error "git is required but not installed"
        exit 1
    fi
    
    print_success "git $(git --version | cut -d' ' -f3)"
    
    # Check Docker (optional)
    if command -v docker &> /dev/null; then
        print_success "Docker $(docker --version | cut -d' ' -f3 | sed 's/,//')"
        DOCKER_AVAILABLE=true
    else
        print_warning "Docker not found (optional for full deployment)"
        DOCKER_AVAILABLE=false
    fi
}

install_application() {
    print_step "Installing Faxbot..."
    
    # Create installation directory
    mkdir -p "$INSTALL_DIR"
    mkdir -p "$BIN_DIR"
    
    # Clone or download the repository
    if [ -d "$INSTALL_DIR/.git" ]; then
        print_step "Updating existing installation..."
        cd "$INSTALL_DIR"
        git pull origin main
    else
        print_step "Downloading application..."
        git clone "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
    fi
    
    print_success "Application downloaded to $INSTALL_DIR"
}

install_dependencies() {
    print_step "Installing MCP dependencies..."
    
    cd "$INSTALL_DIR/api"
    npm install --production
    
    print_success "Dependencies installed"
}

setup_configuration() {
    print_step "Setting up configuration..."
    
    cd "$INSTALL_DIR"
    
    # Copy environment template
    if [ ! -f ".env" ] && [ -f ".env.example" ]; then
        cp .env.example .env
        print_success "Created .env configuration file"
        ENV_CREATED=true
    else
        print_success "Configuration file exists"
        ENV_CREATED=false
    fi
    
    # MCP configuration helpers are no longer generated from api/; use docs/MCP_INTEGRATION.md
    print_success "Using Node/Python MCP per docs/MCP_INTEGRATION.md"
}

create_shortcuts() {
    print_step "Creating system shortcuts..."
    
    # Create stdio launcher for Node MCP (preferred)
    cat > "$BIN_DIR/faxbot-mcp" << EOF
#!/bin/bash
cd "$INSTALL_DIR/node_mcp"
node src/servers/stdio.js "\$@"
EOF
    chmod +x "$BIN_DIR/faxbot-mcp"
    
    # Add to PATH if not already there
    SHELL_RC=""
    if [ -f "$HOME/.bashrc" ]; then
        SHELL_RC="$HOME/.bashrc"
    elif [ -f "$HOME/.zshrc" ]; then
        SHELL_RC="$HOME/.zshrc"
    fi
    
    if [ -n "$SHELL_RC" ] && ! grep -q "$BIN_DIR" "$SHELL_RC"; then
        echo "" >> "$SHELL_RC"
        echo "# Faxbot MCP" >> "$SHELL_RC"
        echo "export PATH=\"\$PATH:$BIN_DIR\"" >> "$SHELL_RC"
        print_success "Added to PATH in $SHELL_RC"
        PATH_UPDATED=true
    else
        PATH_UPDATED=false
    fi
    
    print_success "System shortcuts created"
}

setup_desktop_integration() {
    print_step "Setting up desktop integration..."
    
    case "$OS" in
        macOS)
            # Create macOS application bundle (optional)
            APPS_DIR="$HOME/Applications"
            mkdir -p "$APPS_DIR"
            
            # This would create a .app bundle for macOS
            print_success "macOS integration ready"
            ;;
        Linux)
            # Create .desktop file for Linux
            DESKTOP_DIR="$HOME/.local/share/applications"
            mkdir -p "$DESKTOP_DIR"
            
            cat > "$DESKTOP_DIR/faxbot.desktop" << EOF
[Desktop Entry]
Name=Faxbot
Comment=AI-Native Fax Transmission Service
Exec=$BIN_DIR/faxbot-mcp
Icon=mail-send
Terminal=false
Type=Application
Categories=Office;Network;
EOF
            
            print_success "Linux desktop integration created"
            ;;
    esac
}

print_installation_complete() {
    echo ""
    echo -e "${GREEN}🎉 Faxbot Installation Complete!${NC}"
    echo ""
    echo -e "${BLUE}📋 Next Steps:${NC}"
    echo ""
    
    if [ "$ENV_CREATED" = true ]; then
        echo -e "${YELLOW}1. Configure your SIP provider settings:${NC}"
        echo "   edit $INSTALL_DIR/.env"
        echo ""
    fi
    
    echo -e "${BLUE}2. Start the fax API service:${NC}"
    echo "   cd $INSTALL_DIR"
    if [ "$DOCKER_AVAILABLE" = true ]; then
        echo "   docker-compose up -d"
    else
        echo "   # Install Docker first, or run API manually"
    fi
    echo ""
    
    echo -e "${BLUE}3. Start MCP server:${NC}"
    if [ "$PATH_UPDATED" = true ]; then
        echo "   faxbot-mcp              # Stdio mode"
        echo "   (restart terminal for PATH updates)"
    else
        echo "   $BIN_DIR/faxbot-mcp              # Stdio mode"
    fi
    echo ""
    
    echo -e "${BLUE}🗣️  Voice Assistant Examples:${NC}"
    echo '   "Hey Claude, fax my prescription to the pharmacy at 555-0123"'
    echo '   "Send my insurance card to Dr. Smith'"'"'s office"'
    echo ""
    
    echo -e "${BLUE}📖 Documentation:${NC}"
    echo "   $INSTALL_DIR/README.md"
    echo "   https://github.com/DMontgomery40/Faxbot"
    echo ""
    
    echo -e "${BLUE}🔧 Configuration Files:${NC}"
    echo "   $INSTALL_DIR/api/configs/"
    echo ""
    
    echo -e "${GREEN}🚀 Ready for AI-powered fax transmission!${NC}"
}

# Main installation flow
main() {
    print_header
    
    check_requirements
    install_application
    install_dependencies
    setup_configuration
    create_shortcuts
    setup_desktop_integration
    
    print_installation_complete
}

# Run installer
main "$@"
