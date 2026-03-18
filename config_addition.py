# Add these two lines to your Settings class in config.py:
# (inside the class Settings(BaseSettings): block)

ANTHROPIC_API_KEY:  str         # get from console.anthropic.com
HOSTEL_MOMO_NAME:   str = "UZU HOSTEL"      # name on your MoMo account
HOSTEL_MOMO_NUMBER: str = "0241234567"      # your actual MoMo number

# Also add to .env:
# ANTHROPIC_API_KEY=sk-ant-...
# HOSTEL_MOMO_NAME=UZU HOSTEL
# HOSTEL_MOMO_NUMBER=0241234567
