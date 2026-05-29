import os
import requests
def send_simple_message():
  	return requests.post(
  		"https://api.mailgun.net/v3/sandbox0f573c896815403187d164e2b5a5b2ac.mailgun.org/messages",
  		auth=("api", os.getenv('API_KEY', 'API_KEY')),
  		data={"from": "Mailgun Sandbox <postmaster@sandbox0f573c896815403187d164e2b5a5b2ac.mailgun.org>",
			"to": "Dale Pinn <dale@myguys.co.za>",
  			"subject": "Hello Dale Pinn",
  			"text": "Congratulations Dale Pinn, you just sent an email with Mailgun! You are truly awesome!"})
if __name__ == "__main__":
    send_simple_message()	