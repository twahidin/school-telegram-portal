/**
 * School Portal - Main JavaScript
 */

// ============================================
// Toast Notifications
// ============================================

function showToast(title, message, type = 'info') {
    const toastEl = document.getElementById('notification-toast');
    if (!toastEl) return;
    
    const toastTitle = document.getElementById('toast-title');
    const toastBody = document.getElementById('toast-body');
    
    toastTitle.textContent = title;
    toastBody.textContent = message;
    
    // Update toast style based on type
    toastEl.className = 'toast';
    if (type === 'success') {
        toastEl.classList.add('bg-success', 'text-white');
    } else if (type === 'danger' || type === 'error') {
        toastEl.classList.add('bg-danger', 'text-white');
    } else if (type === 'warning') {
        toastEl.classList.add('bg-warning');
    }
    
    const toast = new bootstrap.Toast(toastEl, { delay: 5000 });
    toast.show();
}

// ============================================
// Chat Functionality
// ============================================

let pollInterval = null;

function initChat(teacherId, lastTime) {
    const messageForm = document.getElementById('message-form');
    const messagesContainer = document.getElementById('messages-container');
    
    if (!messageForm || !messagesContainer) return;
    
    // Scroll to bottom
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
    
    // Handle send message
    messageForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const input = document.getElementById('message-input');
        const message = input.value.trim();
        
        if (!message) return;
        
        try {
            const response = await fetch('/api/send_message', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    teacher_id: teacherId,
                    message: message
                })
            });
            
            const data = await response.json();
            
            if (data.success) {
                // Add message to UI
                addMessage(message, true);
                input.value = '';
                
                // Remove empty state if present
                const emptyState = document.getElementById('empty-state');
                if (emptyState) emptyState.remove();
            } else {
                showToast('Error', data.error || 'Failed to send message', 'danger');
            }
        } catch (error) {
            showToast('Error', 'Failed to send message', 'danger');
        }
    });
    
    // Start polling for new messages
    startPolling(teacherId, lastTime);
}

// Format timestamp to Singapore time (UTC+8)
function formatTimeSGT(timestamp) {
    if (!timestamp) {
        const now = new Date();
        timestamp = now.toISOString();
    }
    
    const date = new Date(timestamp);
    // Convert to Singapore time (UTC+8)
    const sgtOffset = 8 * 60; // 8 hours in minutes
    const utcTime = date.getTime() + (date.getTimezoneOffset() * 60000);
    const sgtTime = new Date(utcTime + (sgtOffset * 60000));
    
    // Format as HH:MM
    const hours = String(sgtTime.getHours()).padStart(2, '0');
    const minutes = String(sgtTime.getMinutes()).padStart(2, '0');
    return `${hours}:${minutes}`;
}

function addMessage(text, isSent, timestamp = null) {
    const container = document.getElementById('messages-container');
    if (!container) return;
    
    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${isSent ? 'sent' : 'received'}`;
    
    const timeStr = formatTimeSGT(timestamp);
    
    msgDiv.innerHTML = `
        <div class="message-content">${escapeHtml(text)}</div>
        <div class="message-time">${timeStr}</div>
    `;
    
    container.appendChild(msgDiv);
    container.scrollTop = container.scrollHeight;
}

// Callback for when new messages are received (can be overridden)
window.onNewMessageReceived = null;

function startPolling(teacherId, lastTime) {
    let since = lastTime || '';
    
    pollInterval = setInterval(async () => {
        try {
            const url = `/api/poll_messages/${teacherId}${since ? `?since=${encodeURIComponent(since)}` : ''}`;
            const response = await fetch(url);
            const data = await response.json();
            
            if (data.messages && data.messages.length > 0) {
                // Remove empty state if present
                const emptyState = document.getElementById('empty-state');
                if (emptyState) emptyState.remove();
                
                data.messages.forEach(msg => {
                    if (!msg.from_student) {
                        addMessage(msg.text, false, msg.timestamp);
                        
                        // Trigger notification callback if set
                        if (window.onNewMessageReceived) {
                            window.onNewMessageReceived(msg.text, teacherId);
                        }
                    }
                    since = msg.timestamp;
                });
            }
        } catch (error) {
            console.error('Polling error:', error);
        }
    }, 3000);
}

function stopPolling() {
    if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
    }
}

// Clean up on page unload
window.addEventListener('beforeunload', stopPolling);

// ============================================
// Assignment Form
// ============================================

function initAssignmentForm(assignmentId) {
    const form = document.getElementById('assignment-form');
    if (!form) return;
    
    // Character count for textareas
    document.querySelectorAll('.answer-input').forEach(textarea => {
        const counter = textarea.closest('.answer-area').querySelector('.char-count');
        if (counter) {
            updateCharCount(textarea, counter);
            textarea.addEventListener('input', () => updateCharCount(textarea, counter));
        }
    });
    
    // Save draft button
    const saveDraftBtn = document.getElementById('save-draft-btn');
    if (saveDraftBtn) {
        saveDraftBtn.addEventListener('click', () => saveDraft(assignmentId));
    }
    
    // Get AI feedback button
    const feedbackBtn = document.getElementById('get-feedback-btn');
    if (feedbackBtn) {
        feedbackBtn.addEventListener('click', () => getAIFeedback(assignmentId));
    }
    
    // Submit button
    const submitBtn = document.getElementById('submit-btn');
    if (submitBtn) {
        submitBtn.addEventListener('click', () => {
            const confirmModal = new bootstrap.Modal(document.getElementById('confirm-modal'));
            confirmModal.show();
        });
    }
    
    // Confirm submit button
    const confirmSubmitBtn = document.getElementById('confirm-submit-btn');
    if (confirmSubmitBtn) {
        confirmSubmitBtn.addEventListener('click', () => submitAssignment(assignmentId));
    }
    
    // Auto-save every 30 seconds
    setInterval(() => {
        saveDraft(assignmentId, true);
    }, 30000);
}

function updateCharCount(textarea, counter) {
    counter.textContent = `${textarea.value.length} characters`;
}

function getAnswers() {
    const answers = {};
    document.querySelectorAll('.answer-input').forEach((textarea, index) => {
        answers[index + 1] = textarea.value;
    });
    return answers;
}

async function saveDraft(assignmentId, silent = false) {
    const answers = getAnswers();
    
    try {
        const response = await fetch(`/assignments/${assignmentId}/save`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ answers })
        });
        
        const data = await response.json();
        
        if (data.success && !silent) {
            showToast('Saved', 'Draft saved successfully', 'success');
        } else if (!data.success && !silent) {
            showToast('Error', data.error || 'Failed to save draft', 'danger');
        }
    } catch (error) {
        if (!silent) {
            showToast('Error', 'Failed to save draft', 'danger');
        }
    }
}

async function getAIFeedback(assignmentId) {
    const answers = getAnswers();
    const feedbackSection = document.getElementById('feedback-section');
    const feedbackContent = document.getElementById('ai-feedback-content');
    const loadingModal = new bootstrap.Modal(document.getElementById('loading-modal'));
    
    // Show loading
    document.getElementById('loading-text').textContent = 'Generating AI feedback...';
    loadingModal.show();
    
    try {
        const response = await fetch(`/assignments/${assignmentId}/feedback`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ answers })
        });
        
        const data = await response.json();
        
        loadingModal.hide();
        
        if (data.success) {
            feedbackContent.innerHTML = data.feedback.replace(/\n/g, '<br>');
            feedbackSection.style.display = 'block';
            feedbackSection.scrollIntoView({ behavior: 'smooth' });
        } else {
            showToast('Error', data.error || 'Failed to get AI feedback', 'danger');
        }
    } catch (error) {
        loadingModal.hide();
        showToast('Error', 'Failed to get AI feedback', 'danger');
    }
}

async function submitAssignment(assignmentId) {
    const answers = getAnswers();
    const loadingModal = new bootstrap.Modal(document.getElementById('loading-modal'));
    const confirmModal = bootstrap.Modal.getInstance(document.getElementById('confirm-modal'));
    
    // Hide confirm modal
    if (confirmModal) confirmModal.hide();
    
    // Show loading
    document.getElementById('loading-text').textContent = 'Submitting assignment...';
    loadingModal.show();
    
    try {
        const response = await fetch(`/assignments/${assignmentId}/submit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ answers })
        });
        
        const data = await response.json();
        
        loadingModal.hide();
        
        if (data.success) {
            showToast('Success', data.message || 'Assignment submitted!', 'success');
            setTimeout(() => {
                window.location.href = `/submissions/${data.submission_id}`;
            }, 1500);
        } else {
            showToast('Error', data.error || 'Failed to submit', 'danger');
        }
    } catch (error) {
        loadingModal.hide();
        showToast('Error', 'Failed to submit assignment', 'danger');
    }
}

// ============================================
// Utility Functions
// ============================================

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatDate(dateString) {
    const date = new Date(dateString);
    return date.toLocaleDateString('en-US', {
        day: 'numeric',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    });
}

// ============================================
// Form Validation
// ============================================

function validateForm(formEl) {
    let isValid = true;
    
    formEl.querySelectorAll('[required]').forEach(input => {
        if (!input.value.trim()) {
            isValid = false;
            input.classList.add('is-invalid');
        } else {
            input.classList.remove('is-invalid');
        }
    });
    
    return isValid;
}

// ============================================
// API Helpers
// ============================================

async function apiRequest(url, method = 'GET', data = null) {
    const options = {
        method,
        headers: {}
    };
    
    if (data) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(data);
    }
    
    try {
        const response = await fetch(url, options);
        const result = await response.json();
        
        if (!response.ok) {
            throw new Error(result.error || 'Request failed');
        }
        
        return result;
    } catch (error) {
        console.error('API request error:', error);
        throw error;
    }
}

// ============================================
// Initialize on DOM Ready
// ============================================

document.addEventListener('DOMContentLoaded', () => {
    // Add active class to current nav item
    const currentPath = window.location.pathname;
    document.querySelectorAll('.nav-link').forEach(link => {
        if (link.getAttribute('href') === currentPath) {
            link.classList.add('active');
        }
    });
    
    // Initialize tooltips
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.forEach(el => new bootstrap.Tooltip(el));
    
    // Initialize popovers
    const popoverTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="popover"]'));
    popoverTriggerList.forEach(el => new bootstrap.Popover(el));
});
