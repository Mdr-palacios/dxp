import os
import django
from dotenv import load_dotenv  # Add this

# 1. Load the .env file BEFORE doing anything else
load_dotenv() 

# 2. Point to settings and setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'powerbuilder_app.settings')
django.setup()

# Now you can safely access your models or keys
# from my_app.models import MyModel