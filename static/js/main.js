document.addEventListener('DOMContentLoaded', () => {
    const yearSelect = document.getElementById('year-select');
    const examSelect = document.getElementById('exam-select');
    const subjectSelect = document.getElementById('subject-select');
    const questionsList = document.getElementById('questions-list');
    const questionText = document.getElementById('question-text');
    const optionsContainer = document.getElementById('options-container');
    const sourceImage = document.getElementById('source-image');
    const imagePlaceholder = document.getElementById('image-placeholder');
    const currentQuestionTitle = document.getElementById('current-question-title');
    const imageBadges = document.getElementById('image-badges');
    const questionTimestamp = document.getElementById('question-timestamp');
    
    const btnSaveCrop = document.getElementById('btn-save-crop');
    const btnNext = document.getElementById('btn-next');
    const btnVerify = document.getElementById('btn-verify');
    const btnInitCrop = document.getElementById('btn-init-crop');
    const btnToggleSidebar = document.getElementById('btn-toggle-sidebar');
    const sidebar = document.querySelector('.sidebar');
    const btnCompleteQuestion = document.getElementById('btn-complete-question');

    // Role Info
    const userRole = document.getElementById('current-user-role').value;

    // Edit UI Elements
    const btnEditText = document.getElementById('btn-edit-text');
    const previewContainer = document.getElementById('preview-container');
    const editContainer = document.getElementById('edit-container');
    const editQuestionTextarea = document.getElementById('edit-question-textarea');
    const editOptionsWrapper = document.getElementById('edit-options-wrapper');
    const btnSaveText = document.getElementById('btn-save-text');
    const btnCancelEdit = document.getElementById('btn-cancel-edit');
    const btnFixAi = document.getElementById('btn-fix-ai');
    
    // Recovery UI Elements
    const recoveryContainer = document.getElementById('recovery-container');
    const recoveryHint = document.getElementById('recovery-hint');
    const uploadZone = document.getElementById('upload-zone');
    const missingFileInput = document.getElementById('missing-file-input');

    // Teacher UI Elements
    const teacherSection = document.getElementById('teacher-section');
    const solutionUploadZone = document.getElementById('solution-upload-zone');
    const solutionFileInput = document.getElementById('solution-file-input');
    const solutionPreview = document.getElementById('solution-preview');
    const solutionImgPreview = document.getElementById('solution-img-preview');
    const solutionUploadPrompt = document.getElementById('solution-upload-prompt');

    btnToggleSidebar.addEventListener('click', () => {
        sidebar.classList.toggle('collapsed');
    });

    let currentExamId = null;
    let currentSubject = null;
    let questionsData = [];
    let currentQuestionIndex = -1;
    let cropper = null;
    let easyMDE = null;
    let optionEditors = {};
    let currentSolutionBase64 = null;
    let allExams = [];

    // Initialize Markdown and KaTeX Options
    marked.setOptions({
        breaks: true,
        gfm: true
    });

    // ── Fetch exams on load ──
    fetch('/api/exams')
        .then(res => res.json())
        .then(exams => {
            allExams = exams;
            populateYearSelect();
            populateExamSelect(); // Show all by default
        });

    function populateYearSelect() {
        const years = [...new Set(allExams.map(e => e.year).filter(y => y))].sort((a, b) => b - a);
        yearSelect.innerHTML = '<option value="">All Years</option>';
        years.forEach(year => {
            const opt = document.createElement('option');
            opt.value = year;
            opt.textContent = year;
            yearSelect.appendChild(opt);
        });
    }

    function populateExamSelect(filteredYear = "") {
        const filtered = filteredYear 
            ? allExams.filter(e => e.year == filteredYear)
            : allExams;
            
        examSelect.disabled = false;
        examSelect.innerHTML = '<option value="" disabled selected>Select an exam...</option>';
        
        if (filtered.length === 0) {
            examSelect.innerHTML = '<option value="" disabled selected>No exams found</option>';
            return;
        }

        filtered.forEach(exam => {
            const opt = document.createElement('option');
            opt.value = exam.id;
            const statusIcon = exam.status === 'complete' ? '✅' : '⏳';
            opt.textContent = `${statusIcon} ${exam.label || `Session ${exam.session_index}`} (${exam.total_questions}Q)`;
            examSelect.appendChild(opt);
        });
    }

    yearSelect.addEventListener('change', (e) => {
        populateExamSelect(e.target.value);
        currentExamId = null;
        currentSubject = null;
        subjectSelect.disabled = true;
        subjectSelect.innerHTML = '<option value="" disabled selected>Select an exam first...</option>';
        questionsList.innerHTML = '';
        clearWorkspace();
    });

    examSelect.addEventListener('change', (e) => {
        currentExamId = e.target.value;
        currentSubject = null;
        questionsData = [];
        currentQuestionIndex = -1;
        
        subjectSelect.disabled = false;
        subjectSelect.innerHTML = '<option value="" disabled selected>Loading subjects...</option>';
        questionsList.innerHTML = '';
        clearWorkspace();
        
        fetch(`/api/subjects?exam_id=${currentExamId}`)
            .then(res => res.json())
            .then(subjects => {
                subjectSelect.innerHTML = '<option value="" disabled selected>Select a subject...</option>';
                subjects.forEach(sub => {
                    const opt = document.createElement('option');
                    opt.value = sub;
                    opt.textContent = sub.replace(/_/g, ' ').toUpperCase();
                    subjectSelect.appendChild(opt);
                });
            });
    });

    subjectSelect.addEventListener('change', (e) => {
        currentSubject = e.target.value;
        loadQuestions(currentSubject);
    });

    function loadQuestions(subject) {
        if (!currentExamId) {
            questionsList.innerHTML = '<p class="placeholder-text">Select an exam first.</p>';
            return;
        }
        questionsList.innerHTML = '<p class="placeholder-text">Loading...</p>';
        fetch(`/api/questions/${subject}?exam_id=${currentExamId}`)
            .then(res => res.json())
            .then(data => {
                questionsData = data.questions;
                renderQuestionsList();
                if (questionsData.length > 0) {
                    selectQuestion(0);
                } else {
                    questionsList.innerHTML = '<p class="placeholder-text">No questions found.</p>';
                }
            })
            .catch(err => {
                questionsList.innerHTML = '<p class="placeholder-text">Failed to load data.</p>';
                clearWorkspace();
            });
    }

    function renderQuestionsList() {
        questionsList.innerHTML = '';
        questionsData.forEach((q, index) => {
            const btn = document.createElement('button');
            btn.className = 'question-btn';
            let statusHTML = '';
            const currentUsername = document.getElementById('current-username').value;
            
            if (q.completed) {
                statusHTML = `<div class="status-indicator" style="background-color: var(--success); color: white; display: flex; align-items: center; justify-content: center; font-size: 8px;" title="Completed">✓</div>`;
            } else if (q.is_missing) {
                statusHTML = `<div class="status-indicator" style="background-color: transparent; border: 2px dashed #ef4444;" title="Missing Question"></div>`;
                btn.style.color = "#ef4444";
                btn.style.opacity = "0.8";
            } else if (q.verified) {
                statusHTML = `<div class="status-indicator verified" title="Verified by ${q.verified_by_username}"></div>`;
            } else if (userRole !== 'teacher' && q.locked_by_username && q.locked_by_username !== currentUsername) {
                statusHTML = `<div class="status-indicator" style="background-color: #ef4444; color: white; display: flex; align-items: center; justify-content: center; font-size: 8px;" title="Locked by ${q.locked_by_username}">🔒</div>`;
            } else if (q.question_image) {
                statusHTML = `<div class="status-indicator has-image"></div>`;
            } else if (q.has_image) {
                statusHTML = `<div class="status-indicator" style="background-color: var(--primary);"></div>`;
            }

            btn.innerHTML = `
                <span>Question ${q.question_number}</span>
                ${statusHTML}
            `;
            btn.onclick = () => selectQuestion(index);
            questionsList.appendChild(btn);
        });
    }

    function selectQuestion(index) {
        if (index < 0 || index >= questionsData.length) return;
        
        // Unlock previous question (only if not a teacher)
        if (userRole !== 'teacher' && currentQuestionIndex !== -1 && currentQuestionIndex !== index) {
            const prevQ = questionsData[currentQuestionIndex];
            const currentUsername = document.getElementById('current-username').value;
            if (!prevQ.locked_by_username || prevQ.locked_by_username === currentUsername) {
                fetch('/api/unlock_question', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ subject: currentSubject, question_number: prevQ.question_number, exam_id: currentExamId })
                });
            }
        }
        
        currentQuestionIndex = index;
        const q = questionsData[index];

        document.querySelectorAll('.question-btn').forEach((btn, i) => {
            btn.classList.toggle('active', i === index);
        });

        currentQuestionTitle.textContent = `${currentSubject.replace(/_/g, ' ').toUpperCase()} - Question ${q.question_number}`;
        
        // Update Timestamp
        questionTimestamp.textContent = q.video_timestamp || '00:00:00';
        
        // Update Image Panel Title based on role
        const imagePanelTitle = document.getElementById('image-panel-title');
        if (userRole === 'teacher') {
            imagePanelTitle.textContent = 'Question Image';
        } else {
            imagePanelTitle.textContent = 'Source Image';
        }

        if (q.is_missing) {
            previewContainer.style.display = 'none';
            editContainer.style.display = 'none';
            recoveryContainer.style.display = 'flex';
            teacherSection.style.display = 'none';
            btnEditText.style.display = 'none';
            btnCompleteQuestion.style.display = 'none';
            recoveryHint.innerHTML = `This question was missed by the automatic extractor.<br><br>Check the video between <strong>${q.prev_timestamp || 'Start'}</strong> and <strong>${q.next_timestamp || 'End'}</strong>.`;
            sourceImage.style.display = 'none';
            imagePlaceholder.style.display = 'block';
            imagePlaceholder.textContent = "Upload screenshot to continue...";
            btnInitCrop.style.display = 'none';
            btnSaveCrop.style.display = 'none';
            imageBadges.innerHTML = '';
            if (cropper) { cropper.destroy(); cropper = null; }
            return;
        }

        recoveryContainer.style.display = 'none';

        const currentUsername = document.getElementById('current-username').value;
        const isLockedByOther = userRole !== 'teacher' && q.locked_by_username && q.locked_by_username !== currentUsername;

        if (isLockedByOther) {
            btnEditText.style.display = 'none';
            btnCompleteQuestion.style.display = 'none';
            if (!document.getElementById('lock-warning')) {
                const warning = document.createElement('div');
                warning.id = 'lock-warning';
                warning.style.position = 'fixed';
                warning.style.bottom = '20px';
                warning.style.right = '20px';
                warning.style.padding = '12px 20px';
                warning.style.backgroundColor = 'rgba(239, 68, 68, 0.9)';
                warning.style.color = 'white';
                warning.style.borderRadius = '8px';
                warning.style.zIndex = '9999';
                warning.style.boxShadow = '0 4px 12px rgba(0,0,0,0.3)';
                warning.style.display = 'flex';
                warning.style.alignItems = 'center';
                warning.style.gap = '10px';
                warning.style.fontSize = '0.9rem';
                warning.style.animation = 'fadeIn 0.3s ease-out';
                warning.innerHTML = `<span>⚠️</span> <span>This question is locked by <strong>${q.locked_by_username}</strong></span>`;
                document.body.appendChild(warning);
                
                // Add simple fade in animation if not present
                if (!document.getElementById('toast-styles')) {
                    const style = document.createElement('style');
                    style.id = 'toast-styles';
                    style.textContent = `
                        @keyframes fadeIn {
                            from { opacity: 0; transform: translateY(10px); }
                            to { opacity: 1; transform: translateY(0); }
                        }
                    `;
                    document.head.appendChild(style);
                }
            }
        } else {
            const w = document.getElementById('lock-warning');
            if (w) w.remove();
            
            // Verifier can edit text, Teacher cannot
            if (userRole === 'verifier' || userRole === 'admin') {
                btnEditText.style.display = 'block';
            } else {
                btnEditText.style.display = 'none';
            }
            
            // Teacher can see "Complete" button
            if (userRole === 'teacher' || userRole === 'admin') {
                btnCompleteQuestion.style.display = 'block';
            } else {
                btnCompleteQuestion.style.display = 'none';
            }

            // Verifier can see "Verify" button
            if ((userRole === 'verifier' || userRole === 'admin') && !q.verified) {
                btnVerify.style.display = 'block';
            } else {
                btnVerify.style.display = 'none';
            }
            
            // Lock it only for non-teachers
            if (userRole !== 'teacher') {
                fetch('/api/lock_question', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ subject: currentSubject, question_number: q.question_number, exam_id: currentExamId })
                });
                q.locked_by_username = currentUsername;
            }
        }

        hideEditMode();
        updatePreview(q);

        // Teacher Section visibility
        if (userRole === 'teacher' || userRole === 'admin') {
            teacherSection.style.display = 'block';
            resetTeacherInputs(q);
        } else {
            teacherSection.style.display = 'none';
        }

        // Badges
        imageBadges.innerHTML = '';
        if (q.completed) {
            imageBadges.innerHTML = `<span class="badge" style="color:var(--success);border-color:var(--success);background:rgba(16,185,129,0.1)">Completed</span>`;
        } else if (q.verified) {
            imageBadges.innerHTML = `<span class="badge" style="color:var(--success);border-color:var(--success);background:rgba(16,185,129,0.1)">Verified by ${q.verified_by_username}</span>`;
        } else if (q.question_image) {
            imageBadges.innerHTML = `<span class="badge" style="color:var(--success);border-color:var(--success);background:rgba(16,185,129,0.1)">Already Cropped</span>`;
        } else if (q.has_image) {
            imageBadges.innerHTML = `<span class="badge">GPT Detected Diagram</span>`;
        }

        loadImage(q);
    }

    function resetTeacherInputs(q) {
        document.querySelectorAll('input[name="correct-answer"]').forEach(radio => {
            radio.checked = (radio.value === q.teacher_answer);
        });
        currentSolutionBase64 = null;
        solutionPreview.style.display = 'none';
        solutionUploadPrompt.style.display = 'block';
        if (q.solution_image) {
            solutionPreview.style.display = 'block';
            solutionUploadPrompt.style.display = 'none';
            solutionImgPreview.src = `/images/${q.exam_id || currentExamId}/${currentSubject}/${q.solution_image}`;
        }
    }

    function renderSafeMarkdown(text) {
        if (!text) return "";
        const placeholders = {};
        let count = 0;
        let processed = text.replace(/\$\$([\s\S]*?)\$\$/g, (match) => {
            const id = `%%MATH_BLOCK_${count++}%%`;
            placeholders[id] = match;
            return id;
        });
        processed = processed.replace(/\$([\s\S]*?)\$/g, (match) => {
            const id = `%%MATH_INLINE_${count++}%%`;
            placeholders[id] = match;
            return id;
        });
        let html = marked.parse(processed);
        for (const [id, math] of Object.entries(placeholders)) {
            html = html.replace(id, math);
        }
        return html;
    }

    function updatePreview(q) {
        if (!q) return;
        questionText.innerHTML = renderSafeMarkdown(q.question_text || "");
        optionsContainer.innerHTML = '';
        if (q.options) {
            ['A', 'B', 'C', 'D'].forEach(opt => {
                if (q.options[opt]) {
                    const optDiv = document.createElement('div');
                    optDiv.className = 'option-item';
                    const optLabel = document.createElement('div');
                    optLabel.className = 'option-letter';
                    optLabel.textContent = `${opt}.`;
                    const optText = document.createElement('div');
                    optText.className = 'option-text';
                    optText.innerHTML = renderSafeMarkdown(q.options[opt]);
                    optDiv.appendChild(optLabel);
                    optDiv.appendChild(optText);
                    optionsContainer.appendChild(optDiv);
                }
            });
        }
        renderMathInElement(questionText, {
            delimiters: [
                {left: '$$', right: '$$', display: true},
                {left: '$', right: '$', display: false},
                {left: '\\(', right: '\\)', display: false},
                {left: '\\[', right: '\\]', display: true}
            ],
            throwOnError: false
        });
        renderMathInElement(optionsContainer, {
            delimiters: [
                {left: '$$', right: '$$', display: true},
                {left: '$', right: '$', display: false},
                {left: '\\(', right: '\\)', display: false},
                {left: '\\[', right: '\\]', display: true}
            ],
            throwOnError: false
        });
    }

    function loadImage(q) {
        if (cropper) { cropper.destroy(); cropper = null; }
        sourceImage.style.display = 'none';
        imagePlaceholder.style.display = 'block';
        imagePlaceholder.textContent = "Source image will appear here...";
        btnInitCrop.style.display = 'none';
        btnSaveCrop.style.display = 'none';

        // Teacher visibility restriction: only show cropped image
        if (userRole === 'teacher') {
            if (q.question_image) {
                let imgUrl = `/images/${q.exam_id || currentExamId}/${currentSubject}/${q.question_image}`;
                sourceImage.src = imgUrl;
                sourceImage.onload = () => {
                    imagePlaceholder.style.display = 'none';
                    sourceImage.style.display = 'block';
                    initViewer();
                };
                sourceImage.onerror = () => {
                    // Fallback: try legacy path
                    sourceImage.src = `/images/${currentSubject}/${q.question_image}`;
                    sourceImage.onerror = null;
                };
            } else {
                imagePlaceholder.textContent = "No diagram for this question.";
            }
            return;
        }

        if (q.image_name || q.question_image) {
            let imgFile = q.question_image || q.image_name;
            let imgUrl = `/images/${q.exam_id || currentExamId}/${currentSubject}/${imgFile}`;
            sourceImage.src = imgUrl;
            sourceImage.onload = () => {
                imagePlaceholder.style.display = 'none';
                sourceImage.style.display = 'block';
                if (userRole !== 'teacher' && q.has_image && !q.question_image) {
                    initCropper();
                } else {
                    if (userRole !== 'teacher') btnInitCrop.style.display = 'block';
                    initViewer();
                }
            };
            sourceImage.onerror = () => {
                // Fallback: try legacy path
                sourceImage.src = `/images/${currentSubject}/${imgFile}`;
                sourceImage.onerror = null; // Prevent infinite loop
            };
        }
    }

    function initViewer() {
        if (cropper) { cropper.destroy(); cropper = null; }
        cropper = new Cropper(sourceImage, {
            viewMode: 1, dragMode: 'move', autoCrop: false, toggleDragModeOnDblclick: false,
            center: false, guides: false, highlight: false, background: false
        });
    }

    function initCropper() {
        const q = questionsData[currentQuestionIndex];
        if (q.question_image && sourceImage.src.includes(q.question_image)) {
            if (cropper) { cropper.destroy(); cropper = null; }
            let imgUrl = `/images/${q.exam_id || currentExamId}/${currentSubject}/${q.image_name}`;
            sourceImage.src = imgUrl;
            sourceImage.onload = () => startCropper();
        } else {
            if (cropper) { cropper.destroy(); cropper = null; }
            startCropper();
        }
    }
    
    function startCropper() {
        btnInitCrop.style.display = 'none';
        btnSaveCrop.style.display = 'block';
        cropper = new Cropper(sourceImage, {
            viewMode: 1, dragMode: 'move', autoCropArea: 0.5, restore: false,
            guides: true, center: true, highlight: false, cropBoxMovable: true,
            cropBox_resizable: true, toggleDragModeOnDblclick: false,
        });
    }

    btnInitCrop.addEventListener('click', initCropper);

    function clearWorkspace() {
        questionText.innerHTML = '<p class="placeholder-text">Question text will appear here...</p>';
        optionsContainer.innerHTML = '';
        if (cropper) { cropper.destroy(); cropper = null; }
        sourceImage.style.display = 'none';
        imagePlaceholder.style.display = 'block';
        imagePlaceholder.textContent = "Source image will appear here...";
        currentQuestionTitle.textContent = 'Select a Subject and Question';
        btnEditText.style.display = 'none';
        btnCompleteQuestion.style.display = 'none';
        teacherSection.style.display = 'none';
        hideEditMode();
    }

    // --- Edit Mode Logic ---
    function showEditMode() {
        const q = questionsData[currentQuestionIndex];
        if (!q) return;
        previewContainer.style.display = 'none';
        editContainer.style.display = 'flex';
        btnEditText.style.display = 'none';
        if (!easyMDE) {
            easyMDE = new EasyMDE({ 
                element: editQuestionTextarea,
                spellChecker: false, maxHeight: "250px", status: false,
                toolbar: ["bold", "italic", "heading", "|", "quote", "unordered-list", "ordered-list", "|", "link", "image", "|", "preview", "side-by-side", "fullscreen", "|", "guide"],
                previewRender: function(plainText, preview) {
                    setTimeout(function() {
                        preview.innerHTML = renderSafeMarkdown(plainText);
                        renderMathInElement(preview, {
                            delimiters: [
                                {left: '$$', right: '$$', display: true}, {left: '$', right: '$', display: false},
                                {left: '\\(', right: '\\)', display: false}, {left: '\\[', right: '\\]', display: true}
                            ],
                            throwOnError: false
                        });
                    }, 0);
                    return "Rendering...";
                }
            });
        }
        easyMDE.value(q.question_text || '');
        editOptionsWrapper.innerHTML = '';
        Object.values(optionEditors).forEach(editor => { if (editor) editor.toTextArea(); });
        optionEditors = {};
        const defaultOptions = ['A', 'B', 'C', 'D'];
        const currentOptions = q.options || {};
        defaultOptions.forEach(opt => {
            const row = document.createElement('div');
            row.className = 'edit-option-row';
            row.innerHTML = `<span class="option-letter" style="margin-top: 10px;">${opt}.</span><div style="flex: 1; max-width: 95%;"><textarea id="edit-opt-${opt}" class="edit-input option-input" data-letter="${opt}" placeholder="Option ${opt} text..."></textarea></div>`;
            editOptionsWrapper.appendChild(row);
            const textarea = document.getElementById(`edit-opt-${opt}`);
            textarea.value = currentOptions[opt] || '';
            optionEditors[opt] = new EasyMDE({
                element: textarea, spellChecker: false, minHeight: "50px", status: false, toolbar: ["bold", "italic", "quote", "|", "preview", "side-by-side"],
                previewRender: function(plainText, preview) {
                    setTimeout(function() {
                        preview.innerHTML = marked.parseInline(plainText);
                        renderMathInElement(preview, {
                            delimiters: [{left: '$$', right: '$$', display: true}, {left: '$', right: '$', display: false}, {left: '\\(', right: '\\)', display: false}, {left: '\\[', right: '\\]', display: true}],
                            throwOnError: false
                        });
                    }, 0);
                    return "Rendering...";
                }
            });
        });
    }

    function hideEditMode() {
        if (questionsData[currentQuestionIndex] && questionsData[currentQuestionIndex].is_missing) return;
        previewContainer.style.display = 'block';
        editContainer.style.display = 'none';
        recoveryContainer.style.display = 'none';
        if (userRole === 'verifier' || userRole === 'admin') btnEditText.style.display = 'block';
    }

    btnEditText.addEventListener('click', showEditMode);
    btnCancelEdit.addEventListener('click', hideEditMode);

    btnSaveText.addEventListener('click', () => {
        const q = questionsData[currentQuestionIndex];
        if (!q) return;
        btnSaveText.textContent = "Saving...";
        btnSaveText.disabled = true;
        const newText = easyMDE.value();
        const newOptions = {};
        ['A', 'B', 'C', 'D'].forEach(opt => { if (optionEditors[opt]) { const val = optionEditors[opt].value().trim(); if (val) newOptions[opt] = val; } });
        fetch('/api/update_text', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ subject: currentSubject, question_number: q.question_number, question_text: newText, options: newOptions, exam_id: currentExamId })
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                q.question_text = newText; q.options = newOptions; q.verified = true;
                q.verified_by_username = document.getElementById('current-username').value;
                selectQuestion(currentQuestionIndex); renderQuestionsList();
            } else alert("Failed to save changes.");
        })
        .finally(() => { btnSaveText.textContent = "Save Changes"; btnSaveText.disabled = false; });
    });

    btnVerify.addEventListener('click', () => {
        const q = questionsData[currentQuestionIndex];
        if (!q) return;
        
        btnVerify.textContent = "Verifying...";
        btnVerify.disabled = true;

        fetch('/api/verify_question', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                subject: currentSubject,
                question_number: q.question_number,
                exam_id: currentExamId
            })
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                q.verified = true;
                q.verified_by_username = document.getElementById('current-username').value;
                renderQuestionsList();
                if (currentQuestionIndex < questionsData.length - 1) selectQuestion(currentQuestionIndex + 1);
            } else alert("Failed to verify question: " + data.error);
        })
        .finally(() => {
            btnVerify.textContent = "Verify Question";
            btnVerify.disabled = false;
        });
    });

    btnCompleteQuestion.addEventListener('click', () => {
        const q = questionsData[currentQuestionIndex];
        if (!q) return;
        const selectedAnswer = document.querySelector('input[name="correct-answer"]:checked');
        if (!selectedAnswer) { alert("Please select the correct answer."); return; }
        
        btnCompleteQuestion.textContent = "Completing...";
        btnCompleteQuestion.disabled = true;

        fetch('/api/submit_solution', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                subject: currentSubject, question_number: q.question_number,
                teacher_answer: selectedAnswer.value, solution_image: currentSolutionBase64,
                exam_id: currentExamId
            })
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                q.completed = true;
                q.teacher_answer = selectedAnswer.value;
                renderQuestionsList();
                if (currentQuestionIndex < questionsData.length - 1) selectQuestion(currentQuestionIndex + 1);
            } else alert("Failed to complete question: " + data.error);
        })
        .finally(() => { btnCompleteQuestion.textContent = "Complete Question"; btnCompleteQuestion.disabled = false; });
    });

    // Solution Upload Logic
    solutionUploadZone.addEventListener('click', () => solutionFileInput.click());
    solutionFileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            const file = e.target.files[0];
            const reader = new FileReader();
            reader.onload = (event) => {
                currentSolutionBase64 = event.target.result;
                solutionImgPreview.src = currentSolutionBase64;
                solutionPreview.style.display = 'block';
                solutionUploadPrompt.style.display = 'none';
            };
            reader.readAsDataURL(file);
        }
    });

    btnSaveCrop.addEventListener('click', () => {
        if (!cropper || currentQuestionIndex === -1) return;
        const q = questionsData[currentQuestionIndex];
        const canvas = cropper.getCroppedCanvas({ fillColor: '#fff', imageSmoothingEnabled: true, imageSmoothingQuality: 'high', });
        if (!canvas) return;
        const base64Image = canvas.toDataURL('image/jpeg', 0.9);
        btnSaveCrop.textContent = "Saving...";
        btnSaveCrop.disabled = true;
        fetch(`/api/crop/${currentSubject}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question_number: q.question_number, image_data: base64Image, exam_id: currentExamId })
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                q.question_image = data.filename; q.has_image = true; renderQuestionsList();
                if (currentQuestionIndex < questionsData.length - 1) selectQuestion(currentQuestionIndex + 1);
            } else alert("Failed to save crop.");
        })
        .finally(() => { btnSaveCrop.textContent = "Save Crop & Next"; btnSaveCrop.disabled = false; });
    });

    btnNext.addEventListener('click', () => { if (currentQuestionIndex < questionsData.length - 1) selectQuestion(currentQuestionIndex + 1); });

    uploadZone.addEventListener('click', () => missingFileInput.click());
    missingFileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            const file = e.target.files[0];
            const q = questionsData[currentQuestionIndex];
            const reader = new FileReader();
            reader.onload = (event) => {
                fetch('/api/upload_missing_question', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ subject: currentSubject, question_number: q.question_number, image_data: event.target.result, exam_id: currentExamId })
                })
                .then(res => res.json())
                .then(data => { if(data.success) loadQuestions(currentSubject); else alert(data.error); });
            };
            reader.readAsDataURL(file);
        }
    });
});
