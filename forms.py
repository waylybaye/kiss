from django import forms

class ResourceForm(forms.ModelForm):
    def __init__(self, request, *args, **kwargs):
        super(ResourceForm, self).__init__(*args, **kwargs)
        self.request = request
