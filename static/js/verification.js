// The markup is rendered by Flask ahead of this file, but the browser does not
// guarantee that this script executes after every form section exists. Bind on
// DOMContentLoaded (falling back to immediately if it already fired) so the
// Continue/Back handlers are always attached to the real elements.
function initVerificationWizard(){
const form=document.getElementById('verificationForm');
if(!form) return;
const steps=[...document.querySelectorAll('.verification-step')];
const indicators=[...document.querySelectorAll('[data-step-indicator]')];
const message=document.getElementById('verificationMessage');
const finalMessage=document.getElementById('verificationMessageFinal');
const submitButton=document.getElementById('submitVerification');
let currentStep=1;
let cameraStream=null;
let capturedFace=null;

function showMessage(element,text,error=true){if(!element)return;element.textContent=text;element.className='form-message '+(error?'is-error':'is-success');element.style.display='block'}
function showStep(step){currentStep=step;steps.forEach(section=>{section.hidden=Number(section.dataset.step)!==step});indicators.forEach(item=>item.classList.toggle('active',Number(item.dataset.stepIndicator)<=step));if(step===3)fillReview();if(step!==2)stopCamera()}
function requireStepOne(){const values=[form.elements.username.value.trim(),form.elements.phone.value.trim(),form.elements.business_address.value.trim(),form.elements.nin_number.value.trim(),form.elements.cac_number.value.trim()];if(values.some(value=>!value)){showMessage(message,'Complete all business and identity details.',true);return false}return true}
function requireStepTwo(){const face=capturedFace||form.elements.face_photo.files[0];if(!face||!form.elements.nin_photo.files[0]||!form.elements.cac_photo.files[0]){showMessage(message,'Capture your face photo and choose both document pictures.',true);return false}return true}
function fillReview(){document.getElementById('reviewUsername').textContent=form.elements.username.value.trim();document.getElementById('reviewPhone').textContent=form.elements.phone.value.trim();document.getElementById('reviewAddress').textContent=form.elements.business_address.value.trim();document.getElementById('reviewNin').textContent=form.elements.nin_number.value.trim();document.getElementById('reviewCac').textContent=form.elements.cac_number.value.trim()}
function previewFile(input,img){const file=input.files[0];if(file){img.src=URL.createObjectURL(file);img.hidden=false}}
async function startCamera(){try{cameraStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'user'},audio:false});const video=document.getElementById('faceVideo');video.srcObject=cameraStream;video.hidden=false;document.getElementById('startCamera').textContent='Camera ready — capture face';}catch(error){showMessage(message,'Camera access was unavailable. Choose a face photo from your device.',true)}}
function captureFace(){const video=document.getElementById('faceVideo');const canvas=document.getElementById('faceCanvas');if(!video.videoWidth||!video.videoHeight){showMessage(message,'Start the camera before capturing your face.',true);return}canvas.width=video.videoWidth;canvas.height=video.videoHeight;canvas.getContext('2d').drawImage(video,0,0);canvas.toBlob(blob=>{if(!blob){showMessage(message,'Could not capture the face photo.',true);return}capturedFace=new File([blob],'face-photo.png',{type:'image/png'});const preview=document.getElementById('facePreview');preview.src=URL.createObjectURL(blob);preview.hidden=false;video.hidden=true;stopCamera();document.getElementById('startCamera').textContent='Face photo captured';showMessage(message,'Face photo captured.',false)},'image/png')}
function stopCamera(){if(cameraStream){cameraStream.getTracks().forEach(track=>track.stop());cameraStream=null}}
document.querySelectorAll('[data-next]').forEach(button=>button.addEventListener('click',()=>{if(currentStep===1&&!requireStepOne())return;if(currentStep===2&&!requireStepTwo())return;showStep(currentStep+1)}));
document.querySelectorAll('[data-prev]').forEach(button=>button.addEventListener('click',()=>showStep(currentStep-1)));
const startCameraButton=document.getElementById('startCamera');
if(startCameraButton)startCameraButton.addEventListener('click',()=>{if(document.getElementById('facePreview').hidden)startCamera();else captureFace()});
const faceInput=document.getElementById('facePhotoInput');
if(faceInput)faceInput.addEventListener('change',event=>{previewFile(event.target,document.getElementById('facePreview'));capturedFace=null});
const ninInput=document.querySelector('input[name="nin_photo"]');
if(ninInput)ninInput.addEventListener('change',event=>previewFile(event.target,document.getElementById('ninPreview')));
const cacInput=document.querySelector('input[name="cac_photo"]');
if(cacInput)cacInput.addEventListener('change',event=>previewFile(event.target,document.getElementById('cacPreview')));
form.addEventListener('submit',async event=>{event.preventDefault();if(!requireStepOne()||!requireStepTwo()||!document.getElementById('verificationConsent').checked){showMessage(finalMessage,'Confirm all details, documents and the consent statement.',true);return}const payload=new FormData(form);if(capturedFace){payload.set('face_photo',capturedFace,'face-photo.png')}submitButton.disabled=true;submitButton.textContent='Submitting…';finalMessage.style.display='none';try{const response=await fetch('/api/verification/submit',{method:'POST',body:payload});const data=await response.json();if(!response.ok)throw new Error(data.error||'Could not submit verification.');showMessage(finalMessage,data.message||'Verification submitted for admin review.',false);setTimeout(()=>window.location.href='/property-manager',900)}catch(error){showMessage(finalMessage,error.message||'Network error. Please try again.',true);submitButton.disabled=false;submitButton.textContent='Submit for review'}finally{if(submitButton.disabled){submitButton.disabled=false;submitButton.textContent='Submit for review'}}});
window.addEventListener('pagehide',stopCamera);
}

if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',initVerificationWizard)}else{initVerificationWizard()}
